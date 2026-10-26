"""Outreach status, performance and exceptions (A13) -- derived on read from the facts
the agents already store; tenant-scoped, parameterised SQL only."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from common.auth import AuthClaims
from common.db import Database

from api.agents.repository import _reject_global
from api.email_agent import repository as email_repo

STATUSES = ("new", "contacted", "engaged", "qualified", "meeting_booked", "human_handoff",
            "invalid_contact", "not_interested", "opted_out")

# R1: one status per contact (with at least one plan), first match wins.
_STATUS_SQL = """
WITH people AS (
    SELECT c.contact_id, c.name, c.email, c.lead_id FROM contacts c
    WHERE c.tenant_id = $1 AND EXISTS (
        SELECT 1 FROM outreach_plans p WHERE p.tenant_id = c.tenant_id
        AND p.contact_id = c.contact_id)
), vals AS (
    SELECT i.contact_id, i.identity_value AS value FROM contact_identities i
    JOIN people USING (contact_id)
    WHERE i.tenant_id = $1 AND i.identity_type IN ('email', 'phone')
    UNION SELECT contact_id, lower(email) FROM people WHERE email IS NOT NULL
), sup AS (
    SELECT v.contact_id, array_agg(DISTINCT s.reason) AS reasons FROM vals v
    JOIN suppression_list s ON s.tenant_id = $1 AND s.value_normalised = v.value
    GROUP BY v.contact_id
), facts AS (
    SELECT p.contact_id, p.name, p.email, p.lead_id,
        coalesce(sup.reasons, '{}') AS reasons,
        EXISTS (SELECT 1 FROM outreach_plans x WHERE x.tenant_id = $1
                AND x.contact_id = p.contact_id AND x.status = 'active') AS running,
        EXISTS (SELECT 1 FROM schedule_events e WHERE e.tenant_id = $1
                AND e.status IN ('booked', 'completed', 'no_show')
                AND ((p.lead_id IS NOT NULL AND e.lead_id = p.lead_id)
                     OR (p.email IS NOT NULL AND lower(e.email) = lower(p.email)))) AS booked,
        EXISTS (SELECT 1 FROM outreach_plans x WHERE x.tenant_id = $1
                AND x.contact_id = p.contact_id AND x.stop_reason = 'human_takeover')
        OR EXISTS (SELECT 1 FROM email_messages m JOIN email_threads t
                   ON t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id
                   WHERE m.tenant_id = $1 AND t.contact_id = p.contact_id
                   AND m.direction = 'inbound' AND m.handled_action = 'human')
        OR EXISTS (SELECT 1 FROM call_sessions s WHERE s.tenant_id = $1
                   AND s.contact_id = p.contact_id AND s.outcome = 'escalated') AS handoff,
        EXISTS (SELECT 1 FROM leads l WHERE l.tenant_id = $1 AND l.lead_id = p.lead_id
                AND l.stage IN ('qualified', 'converted')) AS qualified,
        EXISTS (SELECT 1 FROM email_messages m JOIN email_threads t
                ON t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id
                WHERE m.tenant_id = $1 AND t.contact_id = p.contact_id
                AND m.direction = 'inbound')
        OR EXISTS (SELECT 1 FROM call_sessions s JOIN call_transcript_turns tt
                   ON tt.tenant_id = s.tenant_id AND tt.call_id = s.call_id
                   WHERE s.tenant_id = $1 AND s.contact_id = p.contact_id
                   AND tt.speaker = 'caller') AS engaged,
        EXISTS (SELECT 1 FROM outreach_tasks k JOIN outreach_plans x
                ON x.tenant_id = k.tenant_id AND x.plan_id = k.plan_id
                WHERE k.tenant_id = $1 AND x.contact_id = p.contact_id
                AND k.status = 'completed') AS contacted
    FROM people p LEFT JOIN sup USING (contact_id)
)
SELECT contact_id, name, email, lead_id, CASE
    WHEN reasons && ARRAY['unsubscribe', 'stop', 'dnc_request', 'complaint'] AND NOT running
        THEN 'opted_out'
    WHEN 'not_interested' = ANY(reasons) THEN 'not_interested'
    WHEN booked THEN 'meeting_booked'
    WHEN handoff THEN 'human_handoff'
    WHEN reasons && ARRAY['bounce', 'wrong_number'] AND NOT running THEN 'invalid_contact'
    WHEN qualified THEN 'qualified'
    WHEN engaged THEN 'engaged'
    WHEN contacted THEN 'contacted'
    ELSE 'new' END AS status
FROM facts
"""


async def status_counts(db: Database, claims: AuthClaims) -> dict[str, int]:
    _reject_global(claims)
    rows = await db.fetch(f"SELECT status, count(*) AS n FROM ({_STATUS_SQL}) s "  # noqa: S608
                          "GROUP BY status", claims.tenant_id)
    found = {r["status"]: int(r["n"]) for r in rows}
    return {status: found.get(status, 0) for status in STATUSES}


async def list_statuses(db: Database, claims: AuthClaims, *, status: str | None, limit: int,
                        offset: int) -> list[dict[str, Any]]:
    _reject_global(claims)
    rows = await db.fetch(
        f"SELECT * FROM ({_STATUS_SQL}) s WHERE ($2::text IS NULL OR status = $2) "  # noqa: S608
        "ORDER BY name NULLS LAST, contact_id LIMIT $3 OFFSET $4",
        claims.tenant_id, status, max(1, min(limit, 200)), max(0, offset))
    return [dict(r) for r in rows]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


async def performance(db: Database, claims: AuthClaims, *, now: datetime, days: int,
                      with_costs: bool) -> dict[str, Any]:
    """R2. Leads = contacts whose first plan started in the period."""
    _reject_global(claims)
    t, since = claims.tenant_id, now - timedelta(days=days)
    cohort = await db.fetchrow(
        "WITH cohort AS (SELECT contact_id, min(created_at) AS started FROM outreach_plans "
        "  WHERE tenant_id = $1 GROUP BY contact_id HAVING min(created_at) >= $2), "
        "first_touch AS (SELECT c.contact_id, min(k.completed_at) - c.started AS wait "
        "  FROM cohort c JOIN outreach_plans x ON x.tenant_id = $1 AND x.contact_id = c.contact_id "
        "  JOIN outreach_tasks k ON k.tenant_id = $1 AND k.plan_id = x.plan_id "
        "  AND k.status = 'completed' GROUP BY c.contact_id, c.started), "
        "met AS (SELECT DISTINCT c.contact_id FROM cohort c JOIN contacts p "
        "  ON p.tenant_id = $1 AND p.contact_id = c.contact_id JOIN schedule_events e "
        "  ON e.tenant_id = $1 AND e.status <> 'cancelled' AND e.created_at >= c.started "
        "  AND ((p.lead_id IS NOT NULL AND e.lead_id = p.lead_id) "
        "       OR (p.email IS NOT NULL AND lower(e.email) = lower(p.email)))) "
        "SELECT (SELECT count(*) FROM cohort) AS leads, "
        "(SELECT count(*) FROM first_touch) AS contacted, "
        "(SELECT count(*) FROM met) AS met, "
        "(SELECT percentile_cont(0.5) WITHIN GROUP "
        "   (ORDER BY extract(epoch FROM wait) / 60) FROM first_touch) AS median_minutes",
        t, since)
    assert cohort is not None  # noqa: S101 -- scalar subqueries always return a row
    email = await db.fetchrow(
        "SELECT count(*) FILTER (WHERE m.direction = 'outbound' AND m.status = 'sent' "
        "  AND m.sent_at >= $2) AS sent, "
        "count(DISTINCT t.contact_id) FILTER (WHERE m.direction = 'outbound' "
        "  AND m.status = 'sent' AND m.sent_at >= $2) AS emailed, "
        "count(DISTINCT t.contact_id) FILTER (WHERE m.direction = 'inbound' "
        "  AND m.created_at >= $2) AS replied "
        "FROM email_messages m JOIN email_threads t "
        "ON t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id WHERE m.tenant_id = $1",
        t, since)
    suppressions = {r["reason"]: int(r["n"]) for r in await db.fetch(
        "SELECT reason, count(*) AS n FROM suppression_list WHERE tenant_id = $1 "
        "AND created_at >= $2 GROUP BY reason", t, since)}
    meetings = await db.fetchrow(
        "SELECT count(*) FILTER (WHERE status = 'completed') AS attended, "
        "count(*) FILTER (WHERE status = 'no_show') AS no_show FROM schedule_events "
        "WHERE tenant_id = $1 AND starts_at >= $2", t, since)
    calls = await db.fetchrow(
        "SELECT count(*) AS placed, count(*) FILTER (WHERE answered_at IS NOT NULL "
        "  AND status NOT IN ('voicemail', 'no_answer') "
        "  AND coalesce(amd_result, '') NOT ILIKE '%machine%') AS connected "
        "FROM call_sessions WHERE tenant_id = $1 AND direction = 'outbound' "
        "AND created_at >= $2", t, since)
    assert email is not None and meetings is not None and calls is not None  # noqa: S101
    leads, met, sent = int(cohort["leads"]), int(cohort["met"]), int(email["sent"])
    attended, no_show = int(meetings["attended"]), int(meetings["no_show"])
    out: dict[str, Any] = {
        "days": days, "leads": leads, "contacted": int(cohort["contacted"]),
        "median_response_minutes": (round(float(cohort["median_minutes"]), 1)
                                    if cohort["median_minutes"] is not None else None),
        "emails_sent": sent, "reply_rate": _rate(int(email["replied"]), int(email["emailed"])),
        "meetings": met, "lead_to_meeting": _rate(met, leads),
        "attended": attended, "no_show": no_show, "show_rate": _rate(attended, attended + no_show),
        "bounce_rate": _rate(suppressions.get("bounce", 0), sent),
        "complaint_rate": _rate(suppressions.get("complaint", 0), sent),
        "opt_out_rate": _rate(suppressions.get("unsubscribe", 0), sent),
        "calls_placed": int(calls["placed"]),
        "call_connection_rate": _rate(int(calls["connected"]), int(calls["placed"])),
        "costs": None,
    }
    if with_costs:
        spent = int(await db.fetchval(
            "SELECT coalesce(sum(cost_micros), 0) FROM usage_costs WHERE tenant_id = $1 "
            "AND created_at >= $2", t, since))
        out["costs"] = {"total_micros": spent,
                        "per_lead_micros": spent // leads if leads else None,
                        "per_meeting_micros": spent // met if met else None}
    return out


# -- Meeting outcome (R3) -------------------------------------------------------------------


async def meeting_for_outcome(db: Database, claims: AuthClaims, event_id: str
                              ) -> dict[str, Any] | None:
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT event_id, lead_id, status, starts_at FROM schedule_events "
        "WHERE tenant_id = $1 AND event_id = $2", claims.tenant_id, event_id)
    return dict(row) if row is not None else None


async def set_meeting_outcome(db: Database, claims: AuthClaims, event_id: str,
                              status: str) -> bool:
    """A past meeting (booked or already marked) -> completed / no_show."""
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE schedule_events SET status = $3 WHERE tenant_id = $1 AND event_id = $2 "
        "AND status IN ('booked', 'completed', 'no_show') AND starts_at <= now()",
        claims.tenant_id, event_id, status)
    return str(tag).endswith(" 1")


async def meetings_to_mark(db: Database, claims: AuthClaims, *, limit: int = 20
                           ) -> list[dict[str, Any]]:
    """Past meetings still 'booked' -- nobody said whether they happened."""
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT event_id, lead_id, name, email, starts_at FROM schedule_events "
        "WHERE tenant_id = $1 AND status = 'booked' AND starts_at <= now() "
        "ORDER BY starts_at DESC LIMIT $2", claims.tenant_id, limit)
    return [dict(r) for r in rows]


# -- Exceptions (R4) ------------------------------------------------------------------------


async def exceptions(db: Database, claims: AuthClaims, *, now: datetime) -> dict[str, Any]:
    _reject_global(claims)
    t, week = claims.tenant_id, now - timedelta(days=7)
    tasks = await db.fetch(
        "SELECT k.task_id, k.channel, k.status, k.last_error, k.attempts, k.scheduled_at, "
        "x.contact_id, c.name, x.status AS plan_status FROM outreach_tasks k "
        "JOIN outreach_plans x ON x.tenant_id = k.tenant_id AND x.plan_id = k.plan_id "
        "JOIN contacts c ON c.tenant_id = x.tenant_id AND c.contact_id = x.contact_id "
        "WHERE k.tenant_id = $1 AND k.status IN ('failed', 'escalated') "
        "ORDER BY k.scheduled_at DESC LIMIT 100", t)
    events = await db.fetch(
        "SELECT event_id, kind, attempts, last_error, created_at FROM agent_events "
        "WHERE tenant_id = $1 AND status = 'failed' ORDER BY created_at DESC LIMIT 100", t)
    blocked = await db.fetch(
        "SELECT error_code, count(*) AS n FROM agent_tool_calls WHERE tenant_id = $1 "
        "AND tool = 'compliance_gate' AND NOT ok AND created_at >= $2 "
        "GROUP BY error_code ORDER BY n DESC", t, week)
    needs_human = await db.fetchval(
        "SELECT count(*) FROM notification_events WHERE tenant_id = $1 "
        "AND kind IN ('agent_email_needs_human', 'agent_call_needs_human') "
        "AND created_at >= $2", t, week)
    intake = await db.fetchval(
        "SELECT count(*) FROM lead_intake_events WHERE tenant_id = $1 "
        "AND status IN ('rejected', 'notice')", t)
    return {"tasks": [dict(r) for r in tasks], "dead_events": [dict(r) for r in events],
            "blocked_7d": {str(r["error_code"]): int(r["n"]) for r in blocked},
            "needs_human_7d": int(needs_human), "intake_queue": int(intake)}


async def retry_task(db: Database, claims: AuthClaims, task_id: str) -> str | None:
    """Failed/escalated step -> scheduled now with fresh attempts. ``None`` = not found;
    ``"PLAN_NOT_ACTIVE"`` = its plan has ended."""
    _reject_global(claims)
    row = await db.fetchrow(
        "UPDATE outreach_tasks k SET status = 'scheduled', scheduled_at = now(), attempts = 0, "
        "last_error = 'RETRIED_BY_ADMIN' FROM outreach_plans x "
        "WHERE x.tenant_id = k.tenant_id AND x.plan_id = k.plan_id AND x.status = 'active' "
        "AND k.tenant_id = $1 AND k.task_id = $2 AND k.status IN ('failed', 'escalated') "
        "RETURNING k.task_id", claims.tenant_id, task_id)
    if row is not None:
        # An e-mail that failed to send is sent again (same message, same Message-ID).
        message = await email_repo.get_message_for_task(db, claims, task_id)
        if message is not None and message.status == "failed":
            await email_repo.set_status(db, claims, message.message_id, status="approved",
                                        from_statuses=("failed",))
        return "scheduled"
    exists = await db.fetchval(
        "SELECT 1 FROM outreach_tasks WHERE tenant_id = $1 AND task_id = $2 "
        "AND status IN ('failed', 'escalated')", claims.tenant_id, task_id)
    return "PLAN_NOT_ACTIVE" if exists else None


async def dismiss_task(db: Database, claims: AuthClaims, task_id: str) -> bool:
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE outreach_tasks SET status = 'cancelled', last_error = 'DISMISSED_BY_ADMIN' "
        "WHERE tenant_id = $1 AND task_id = $2 AND status IN ('failed', 'escalated')",
        claims.tenant_id, task_id)
    return str(tag).endswith(" 1")


async def retry_event(db: Database, claims: AuthClaims, event_id: str) -> bool:
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE agent_events SET status = 'pending', attempts = 0, available_at = now(), "
        "last_error = 'RETRIED_BY_ADMIN' WHERE tenant_id = $1 AND event_id = $2 "
        "AND status = 'failed'", claims.tenant_id, event_id)
    return str(tag).endswith(" 1")
