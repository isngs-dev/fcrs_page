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


ACK_INTENT = ("Acknowledge their enquiry: thank them, give a short, useful overview related to "
              "what they asked about, and offer a time with our team.")
HELPFUL_INTENT = ("Day-2 follow-up: share something genuinely helpful related to their need, "
                  "and offer a short call.")
SECOND_CALL_INTENT = "Second call: answer questions and offer to book a meeting."
FINAL_INTENT = "Last friendly check-in; easy way to book or opt out."
# With a call first, the acknowledgment waits this long: a call that connects turns it into
# the thank-you (``after_call``), so the lead gets one email, not an acknowledgment AND a
# thank-you minutes apart; an unanswered call still gets it within 15 minutes.
ACK_AFTER_CALL = timedelta(minutes=15)


def inbound_steps(settings: AgentSettings, call_intent: str | None = None) -> list[AgentTask]:
    """Plan 1.4 (2026-11-17), the approved new-enquiry sequence: day 0 acknowledgment
    (alongside a call when they asked for one), day 2 helpful email, day 4 second permitted
    call, day 7 final email -- then nothing more. Each call is one attempt: the day-4 call
    is the retry. Consent, window, gap and touch limits still apply (``policy.validate``)."""
    start = timedelta(minutes=settings.first_inbound_email_delay_minutes)
    if call_intent:  # a call that connects turns the acknowledgment into its thank-you
        start = max(start, ACK_AFTER_CALL)
    first = ([AgentTask(channel="call", offset=timedelta(0), goal="book_meeting",
                        intent=call_intent, max_attempts=1)] if call_intent else [])
    return [*first,
            AgentTask(channel="email", offset=start, goal="acknowledge", intent=ACK_INTENT,
                      max_attempts=3),
            AgentTask(channel="email", offset=start + _DAY, goal="follow_up",
                      intent=HELPFUL_INTENT, max_attempts=3),
            AgentTask(channel="call", offset=start + 2 * _DAY, goal="book_meeting",
                      intent=SECOND_CALL_INTENT, max_attempts=1),
            AgentTask(channel="email", offset=start + timedelta(days=7), goal="re_engage",
                      intent=FINAL_INTENT, max_attempts=3)]


def default_plan(kind: PlanKind, settings: AgentSettings) -> OutreachPlan:
    """Inbound: the day 0/2/4/7 sequence (``inbound_steps``). Outbound: email d0, call d2,
    email d4."""
    if kind == "inbound_auto":
        return OutreachPlan(kind=kind, steps=inbound_steps(settings))
    start = timedelta(0)
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
                    "them, follow up on the service they picked and the message they wrote, "
                    "answer questions and offer to book a meeting.")


CHAT_CALL_INTENT = ("They were just chatting with us on our website and shared their number: "
                    "thank them, pick up where the chat left off, answer questions and offer to "
                    "book a meeting.")
# A chat call waits until the visitor has gone this quiet (or said goodbye) -- dispatch.
# Owner, 2026-11-17: 30 s (was 2 min) -- the call must not depend on a goodbye being typed.
CHAT_IDLE = timedelta(seconds=30)


def call_first_steps(settings: AgentSettings, intent: str = FORM_CALL_INTENT
                     ) -> list[AgentTask]:
    """A website form (or a chat) that asked for a call: ring right away (speed to lead),
    then the day 0/2/4/7 sequence. The gate still keeps the call inside the contact's window."""
    return inbound_steps(settings, call_intent=intent)


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
