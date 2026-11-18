"""What happens after a meeting is marked in the console (plan 2.1).

No-show (owner, 2026-11-18): a friendly reschedule email now, one call the next business day
for anyone who agreed to calls, a final email on day 3 -- then nothing more. The usual gate
still applies (consent, calling window, gap, touch budget, opt-outs); booking again or a
reply stops it like any plan. Marked "attended" afterwards (a correction) stops it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from common.auth import AuthClaims
from common.db import Database
from common.errors import ConflictError

from api.agents import planner
from api.agents.models import AgentTask
from api.agents.outreach_repository import get_active_plan, has_plan_since, stop_plan
from api.audit.repository import record_audit
from api.contacts.repository import (
    get_contact,
    get_contact_by_lead_id,
    get_contact_id_by_identity,
    get_contact_timezone,
)
from api.leads.assignment import select_next_agent
from api.leads.repository import get_lead
from api.opportunities.config_repository import get_opportunity_config
from api.opportunities.repository import create_opportunity, list_opportunities
from api.scheduling.repository import get_availability, get_event

NO_SHOW_EMAIL_INTENT = (
    "They booked a call with our team but weren't able to join. Kindly say we missed them, no "
    "problem at all, and make it easy to pick a new time. Warm, short, no guilt and no pressure.")
NO_SHOW_CALL_INTENT = (
    "They booked a call with our team but couldn't make it. Ask kindly whether they'd still "
    "like to talk and offer a new time, or whether we should close out the request for now. "
    "Respect a no.")
NO_SHOW_FINAL_INTENT = (
    "Last friendly note after a missed call: an easy way to book a new time, or to tell us "
    "they're all set. After this we stop.")


AFTER_MEETING_INTENT = (
    "Thank them for meeting with our team. Briefly recap what they told us they're looking for, "
    "in their own words, and say the team will follow up with next steps. Do not claim anything "
    "was agreed or decided in the meeting: a person from the team reviews and completes this "
    "email before it is sent.")


def next_business_day(now: datetime, timezone: str) -> timedelta:
    """Whole days from ``now`` to the next Monday-Friday, counted in ``timezone``."""
    local = now.astimezone(ZoneInfo(timezone))
    days = 1
    while (local + timedelta(days=days)).weekday() >= 5:  # Saturday, Sunday
        days += 1
    return timedelta(days=days)


def no_show_steps(call_after: timedelta) -> list[AgentTask]:
    return [
        AgentTask(channel="email", offset=timedelta(0), goal="re_engage",
                  intent=NO_SHOW_EMAIL_INTENT, max_attempts=3),
        AgentTask(channel="call", offset=call_after, goal="book_meeting",
                  intent=NO_SHOW_CALL_INTENT, max_attempts=1),
        AgentTask(channel="email", offset=timedelta(days=3), goal="re_engage",
                  intent=NO_SHOW_FINAL_INTENT, max_attempts=3),
    ]


def _kind(plan: dict[str, Any]) -> str | None:
    """The plan kind, from the stored plan (``outreach_plans.plan``)."""
    stored = plan.get("plan") or {}
    return stored.get("kind") if isinstance(stored, dict) else None


async def _contact_for(db: Database, claims: AuthClaims, event: Any) -> str | None:
    if event.lead_id:
        contact = await get_contact_by_lead_id(db, claims, event.lead_id)
        if contact is not None:
            return contact.contact_id
    if event.email:
        return await get_contact_id_by_identity(db, claims, identity_type="email",
                                                identity_value=event.email)
    return None


async def handle_no_show(db: Database, claims: AuthClaims, payload: dict[str, Any]) -> str:
    event = await get_event(db, claims, payload["event_id"])
    if event is None or event.status != "no_show":
        return "NOT_A_NO_SHOW"  # gone, or corrected before we got here
    contact_id = await _contact_for(db, claims, event)
    if contact_id is None:
        return "NO_CONTACT"
    active = await get_active_plan(db, claims, contact_id)
    if active is not None and _kind(active) == "no_show_recovery":
        return "ALREADY_RECOVERING"  # marked twice
    if active is not None:
        await stop_plan(db, claims, active["plan_id"], reason="meeting_no_show")
    zone = await get_contact_timezone(db, claims, contact_id)
    if zone is None:
        availability = await get_availability(db, claims)
        zone = availability.timezone if availability else "UTC"
    now = datetime.now(UTC)
    try:
        outcome = await planner.create_plan(
            db, claims, contact_id=contact_id, kind="no_show_recovery", goal="reschedule",
            now=now, steps=no_show_steps(next_business_day(now, zone)))
    except ConflictError:
        return "PLAN_ALREADY_ACTIVE"
    return "planned" if outcome.plan_id else "NO_ALLOWED_CHANNEL"


async def _owner(db: Database, claims: AuthClaims, contact: Any) -> str | None:
    """Who owns the deal: the lead's agent, else the contact's owner, else the next agent in
    the client's round-robin (``None`` when that is off -- the console shows it unassigned)."""
    if contact.lead_id:
        lead = await get_lead(db, claims, contact.lead_id)
        if lead is not None and lead.assigned_agent_id:
            return str(lead.assigned_agent_id)
    if contact.owner_agent_id:
        return str(contact.owner_agent_id)
    return await select_next_agent(db, claims)


async def _ensure_opportunity(db: Database, claims: AuthClaims, contact_id: str) -> str:
    """One open opportunity per contact: an existing open one is kept, never duplicated."""
    existing, _ = await list_opportunities(db, claims, contact_id=contact_id, open_only=True,
                                           limit=1)
    if existing:
        return "kept"
    contact = await get_contact(db, claims, contact_id)
    if contact is None:
        return "NO_CONTACT"
    config = await get_opportunity_config(db, claims)
    opportunity_id = await create_opportunity(
        db, claims, contact_id=contact_id, account_id=contact.account_id,
        name=contact.name or contact.email or "New opportunity", amount=None,
        currency=config.currency, owner_agent_id=await _owner(db, claims, contact))
    await record_audit(db, claims, action="opportunity.created_after_meeting",
                       target_type="opportunity", target_id=opportunity_id,
                       metadata={"contact_id": contact_id})
    return "created"


async def handle_attended(db: Database, claims: AuthClaims, payload: dict[str, Any]) -> str:
    """Plan 2.2: they came. A running plan (a no-show recovery, a correction) stops; an open
    opportunity is created with a sales owner; a thank-you is DRAFTED for a person to review
    and complete -- the AI wasn't in the meeting (``email_agent.service`` never auto-sends
    an ``after_meeting`` email)."""
    event = await get_event(db, claims, payload["event_id"])
    if event is None:
        return "EVENT_GONE"
    contact_id = await _contact_for(db, claims, event)
    if contact_id is None:
        return "NO_CONTACT"
    active = await get_active_plan(db, claims, contact_id)
    if active is not None and _kind(active) != "after_meeting":
        await stop_plan(db, claims, active["plan_id"], reason="meeting_attended")
    opportunity = await _ensure_opportunity(db, claims, contact_id)
    if await has_plan_since(db, claims, contact_id, kind="after_meeting",
                            since=event.starts_at):
        return f"opportunity:{opportunity},thank_you:already"
    try:
        outcome = await planner.create_plan(
            db, claims, contact_id=contact_id, kind="after_meeting", goal="thank_you",
            now=datetime.now(UTC), steps=[AgentTask(
                channel="email", offset=timedelta(0), goal="thank_you",
                intent=AFTER_MEETING_INTENT, max_attempts=3)])
    except ConflictError:
        return f"opportunity:{opportunity},thank_you:already"
    drafted = "drafted" if outcome.plan_id else "NO_ALLOWED_CHANNEL"
    return f"opportunity:{opportunity},thank_you:{drafted}"
