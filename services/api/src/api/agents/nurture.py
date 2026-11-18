"""Nurture for leads who aren't ready yet (plan 2.3).

The housekeeping sweep (every 5 minutes, ``agents.reap_stale``) finds new-enquiry and no-show
sequences that have run out -- nothing left to send, the last step closed over a day ago --
marks them ``completed`` (until now they stayed ``active`` forever), and when the lead has no
booking coming up and told us they'll start later, starts a ``nurture`` plan: a light check-in
email at their timeline and a last one 30 days on, then nothing. Emails only; the usual gate
(consent, opt-outs, email window, touch budget) applies at send time.

"Later" is read from the points the client gave the answer to its urgency question, so an
edited question still works: top points -> ready (no nurture); 40-99 -> about a month; under
40 -> about three months; no answer -> no stated timeline, no nurture.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from common.auth import AuthClaims
from common.db import Database
from common.errors import ConflictError
from common.logging import get_logger

from api.agents import planner
from api.agents.dispatch import system_claims
from api.agents.models import AgentTask
from api.agents.outreach_repository import complete_plan, finished_plans_system
from api.contacts.repository import get_contact
from api.qualification import repository as qualification
from api.scheduling.repository import get_upcoming_for_contact

_log = get_logger(__name__)

NURTURE_INTENT = (
    "They told us they're planning to start later. A light, helpful check-in: one useful idea "
    "related to what they wanted, and an easy way to talk when the time is right. No pressure.")
NURTURE_FINAL_INTENT = (
    "A last gentle check-in for now: if the timing still isn't right, that's completely fine; "
    "an easy way to book a call, or to tell us they're no longer interested.")
GRACE = timedelta(days=1)  # a sequence must have been quiet this long before it is "finished"


def nurture_delay(points: int | None) -> timedelta | None:
    if points is None or points >= 100:
        return None
    return timedelta(days=30) if points >= 40 else timedelta(days=90)


def nurture_steps(delay: timedelta) -> list[AgentTask]:
    return [
        AgentTask(channel="email", offset=delay, goal="re_engage", intent=NURTURE_INTENT,
                  max_attempts=3),
        AgentTask(channel="email", offset=delay + timedelta(days=30), goal="re_engage",
                  intent=NURTURE_FINAL_INTENT, max_attempts=3),
    ]


async def _urgency_points(db: Database, claims: AuthClaims, contact_id: str) -> int | None:
    config = await qualification.get_config(db, claims)
    record = await qualification.get_record(db, claims, contact_id)
    for question in config.questions:
        if question.category == "urgency" and question.key in record.answers:
            choice = record.answers[question.key].get("choice")
            return next((c.points for c in question.choices if c.label == choice), None)
    return None


async def sweep_system(db: Database, *, now: datetime, limit: int = 100) -> int:
    """System-scoped (housekeeping): complete finished sequences, start the nurture ones.
    Each plan is handled under claims rebuilt from ITS tenant. Returns nurture plans made."""
    started = 0
    for row in await finished_plans_system(db, quiet_since=now - GRACE, limit=limit):
        claims = system_claims(row["tenant_id"])
        if not await complete_plan(db, claims, row["plan_id"]):
            continue  # raced: stopped or completed meanwhile
        contact = await get_contact(db, claims, row["contact_id"])
        if contact is None:
            continue
        if await get_upcoming_for_contact(db, claims, lead_id=contact.lead_id,
                                          email=contact.email) is not None:
            continue  # a booking is coming up: no nurture
        delay = nurture_delay(await _urgency_points(db, claims, contact.contact_id))
        if delay is None:
            continue
        try:
            outcome = await planner.create_plan(
                db, claims, contact_id=contact.contact_id, kind="nurture", goal="nurture",
                now=now, steps=nurture_steps(delay))
        except ConflictError:
            continue  # another plan started meanwhile (they replied, booked, ...)
        started += 1 if outcome.plan_id else 0
    if started:
        _log.info("nurture plans started", extra={"event": "nurture_started"})
    return started
