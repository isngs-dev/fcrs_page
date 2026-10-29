"""Pure policy validator: a proposed plan in, the plan policy allows out.

Policy wins over the proposal: steps on disabled or unconsented channels are
dropped, calls without a contact timezone are dropped, steps beyond the touch
budget are dropped, offsets move later to respect the minimum gap and the
contact-local calling window. Every change is a ``PolicyNote``. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api.agents.compliance import next_window_open
from api.agents.models import AgentTask, OutreachPlan, PolicyNote
from api.agents.repository import AgentSettings


@dataclass(frozen=True)
class GateInputs:
    """What the planner looked up about the contact (no PII)."""

    consent_ok: dict[str, bool]  # channel -> latest consent allows it (A1 D5)
    timezone: str | None
    touches: int
    last_touch_at: datetime | None


def _zone(name: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if name else None
    except ZoneInfoNotFoundError:
        return None


def validate(
    plan: OutreachPlan, settings: AgentSettings, inputs: GateInputs, now: datetime,
) -> tuple[OutreachPlan | None, list[PolicyNote]]:
    notes: list[PolicyNote] = []
    kept: list[AgentTask] = []
    tz = _zone(inputs.timezone)
    gap = timedelta(hours=settings.min_hours_between_touches)
    budget = settings.max_touches_per_contact - inputs.touches
    previous = inputs.last_touch_at
    # A9: a dropped step gives its slot (the time until the next step) back, so the kept
    # steps keep the template's rhythm and the first one starts when the plan starts.
    freed = timedelta(0)

    for index, step in enumerate(plan.steps):
        code = None
        if step.channel not in settings.enabled_channels:
            code = "CHANNEL_DISABLED"
        elif not inputs.consent_ok.get(step.channel, False):
            code = "NO_CONSENT"
        elif step.channel == "call" and tz is None:
            code = "CONTACT_TIMEZONE_UNKNOWN"
        elif len(kept) >= budget:
            code = "MAX_TOUCHES"
        if code is not None:
            notes.append(PolicyNote(step_index=index, code=code))
            if index + 1 < len(plan.steps):
                freed += plan.steps[index + 1].offset - step.offset
            continue

        at = now + step.offset - freed
        gap_exempt = plan.kind == "inbound_auto" and index == 0  # they just reached out
        if previous is not None and not gap_exempt and at < previous + gap:
            at = previous + gap
            notes.append(PolicyNote(step_index=index, code="GAP_CLAMPED", detail=at.isoformat()))
        if step.channel == "call" and tz is not None:
            opened = next_window_open(at, tz, settings)
            if opened != at:
                at = opened
                notes.append(
                    PolicyNote(step_index=index, code="WINDOW_SHIFTED", detail=at.isoformat()))
        kept.append(step.model_copy(update={"offset": at - now}))
        previous = at

    if not kept:
        notes.append(PolicyNote(step_index=None, code="NO_ALLOWED_CHANNEL"))
        return None, notes
    return plan.model_copy(update={"steps": kept}), notes
