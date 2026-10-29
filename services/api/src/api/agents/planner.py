"""Planner: default cadence (A1 D3) -> policy -> campaign + plan + tasks in one transaction."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from common.auth import AuthClaims
from common.db import Database
from common.errors import NotFoundError

from api.agents.compliance import consent_allows
from api.agents.models import AgentTask, ConsentRef, OutreachPlan, PlanKind, PolicyNote
from api.agents.outreach_repository import (
    PlannedTask,
    create_campaign,
    create_plan_with_tasks,
    touch_stats,
)
from api.agents.policy import GateInputs, validate
from api.agents.repository import AgentSettings, get_agent_settings, latest_consent
from api.contacts.repository import get_contact, get_contact_timezone

_DAY = timedelta(days=2)


def default_plan(kind: PlanKind, settings: AgentSettings) -> OutreachPlan:
    """Inbound: email +delay, call +2 d, email +2 d. Outbound: email d0, call d2, email d4."""
    start = (timedelta(minutes=settings.first_inbound_email_delay_minutes)
             if kind == "inbound_auto" else timedelta(0))
    return OutreachPlan(kind=kind, steps=[
        AgentTask(channel="email", offset=start, goal="follow_up",
                  intent="Follow up on their enquiry and offer help or a meeting.",
                  max_attempts=3),
        AgentTask(channel="call", offset=start + _DAY, goal="book_meeting",
                  intent="Answer questions and offer to book a meeting.",
                  max_attempts=settings.max_call_attempts),
        AgentTask(channel="email", offset=start + 2 * _DAY, goal="re_engage",
                  intent="Last friendly check-in; easy way to book or opt out.",
                  max_attempts=3),
    ])


FORM_CALL_INTENT = ("They just filled in the contact form on our website a moment ago: thank "
                    "them, ask what they need, answer questions and offer to book a meeting.")


def call_first_steps(settings: AgentSettings) -> list[AgentTask]:
    """A website form that asked for a call: ring right away (speed to lead), then the
    usual inbound emails. The policy still keeps the call inside the contact's window."""
    email, call, last = default_plan("inbound_auto", settings).steps
    return [call.model_copy(update={"offset": timedelta(0), "intent": FORM_CALL_INTENT}),
            email, last]


@dataclass(frozen=True)
class PlanOutcome:
    plan_id: str | None
    notes: list[PolicyNote]


async def create_plan(
    db: Database,
    claims: AuthClaims,
    *,
    contact_id: str,
    kind: PlanKind,
    goal: str | None,
    now: datetime,
    campaign_id: str | None = None,
    channels: list[str] | None = None,
    steps: list[AgentTask] | None = None,
) -> PlanOutcome:
    """Plan outreach for a contact. ``plan_id`` is ``None`` when policy allows nothing
    (``NO_ALLOWED_CHANNEL`` in the notes). A second active plan -> ``PLAN_ALREADY_ACTIVE``.

    ``campaign_id`` puts the plan in an existing campaign (one per import/command,
    A5 J8) instead of a new one; ``channels`` limits the plan to those channels."""
    if await get_contact(db, claims, contact_id) is None:
        raise NotFoundError("Contact not found.", code="CONTACT_NOT_FOUND")
    settings = await get_agent_settings(db, claims)
    if channels is not None:
        settings = replace(settings, enabled_channels=tuple(
            ch for ch in settings.enabled_channels if ch in channels))
    consents = {ch: await latest_consent(db, claims, contact_id, ch) for ch in ("email", "call")}
    stats = await touch_stats(db, claims, contact_id, now=now)
    inputs = GateInputs(
        consent_ok={ch: consent_allows(ch, c) for ch, c in consents.items()},
        timezone=await get_contact_timezone(db, claims, contact_id),
        touches=stats.touches,
        last_touch_at=stats.last_touch_at,
    )
    proposal = OutreachPlan(kind=kind, steps=steps) if steps else default_plan(kind, settings)
    plan, notes = validate(proposal, settings, inputs, now)
    if plan is None:
        return PlanOutcome(plan_id=None, notes=notes)

    used = {s.channel for s in plan.steps}
    plan = plan.model_copy(update={"lawful_basis": {
        ch: ConsentRef(consent_id=c.consent_id, basis=c.basis)
        for ch, c in consents.items() if c is not None and ch in used
    }})
    tasks = [
        PlannedTask(step_index=i, channel=s.channel, goal=s.goal, intent=s.intent,
                    scheduled_at=now + s.offset, max_attempts=s.max_attempts)
        for i, s in enumerate(plan.steps)
    ]
    async with db.acquire() as conn, conn.transaction():
        if campaign_id is None:
            campaign_id = await create_campaign(
                conn, claims, name=f"{kind} {now:%Y-%m-%d}", kind=kind, goal=goal)
        plan_id = await create_plan_with_tasks(
            conn, claims, campaign_id=campaign_id, contact_id=contact_id,
            plan=plan.model_dump(mode="json"),
            policy_notes=[n.model_dump() for n in notes], tasks=tasks,
        )
    return PlanOutcome(plan_id=plan_id, notes=notes)
