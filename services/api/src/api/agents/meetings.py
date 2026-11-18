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
from api.agents.outreach_repository import get_active_plan, stop_plan
from api.contacts.repository import (
    get_contact_by_lead_id,
    get_contact_id_by_identity,
    get_contact_timezone,
)
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


async def handle_attended(db: Database, claims: AuthClaims, payload: dict[str, Any]) -> str:
    """They showed after all: a running no-show recovery stops."""
    event = await get_event(db, claims, payload["event_id"])
    if event is None:
        return "EVENT_GONE"
    contact_id = await _contact_for(db, claims, event)
    if contact_id is None:
        return "NO_CONTACT"
    active = await get_active_plan(db, claims, contact_id)
    if active is not None and _kind(active) == "no_show_recovery":
        await stop_plan(db, claims, active["plan_id"], reason="meeting_attended")
        return "recovery_stopped"
    return "nothing_to_stop"
