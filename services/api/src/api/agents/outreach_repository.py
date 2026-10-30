"""Outreach campaigns / plans / tasks (migration 0066).

Tenant methods take ``AuthClaims`` and filter by ``claims.tenant_id``. The
``claim_*`` / ``*_system`` functions are system-scoped (Beat-driven, pattern
``scheduling/reminder_repository.py:claim_due_reminders``): they return rows
whose ``tenant_id`` the caller turns into claims before any tenant call.

Functions that must join a caller's transaction take ``db`` typed as
``Database`` but accept the ``conn`` from ``db.acquire()`` too (same
``execute``/``fetch*`` surface).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from asyncpg.exceptions import UniqueViolationError
from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import ConflictError

from api.agents.repository import _reject_global

_NON_TERMINAL = ("pending", "scheduled", "in_progress")


@dataclass(frozen=True)
class PlannedTask:
    step_index: int
    channel: str
    goal: str
    intent: str
    scheduled_at: datetime
    max_attempts: int


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    plan_id: str
    step_index: int
    channel: str
    goal: str
    intent: str
    scheduled_at: datetime
    status: str
    attempts: int
    max_attempts: int
    completed_at: datetime | None
    result: dict[str, Any] | None
    last_error: str | None


_TASK_SELECT = (
    "SELECT task_id, plan_id, step_index, channel, goal, intent, scheduled_at, status, attempts, "
    "max_attempts, completed_at, result, last_error FROM outreach_tasks "
)


def idempotency_key(plan_id: str, step_index: int) -> str:
    return hashlib.sha256(f"{plan_id}:{step_index}".encode()).hexdigest()


async def create_campaign(
    db: Database, claims: AuthClaims, *, name: str, kind: str, goal: str | None,
) -> str:
    _reject_global(claims)
    campaign_id = uuid4().hex
    await db.execute(
        "INSERT INTO outreach_campaigns (tenant_id, campaign_id, name, kind, goal, created_by) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        claims.tenant_id, campaign_id, name, kind, goal, claims.subject,
    )
    return campaign_id


async def create_plan_with_tasks(
    db: Database,
    claims: AuthClaims,
    *,
    campaign_id: str,
    contact_id: str,
    plan: dict[str, Any],
    policy_notes: list[dict[str, Any]],
    tasks: list[PlannedTask],
) -> str:
    """Insert the plan and its tasks. Run inside one transaction (the planner does).

    A second active plan for the contact -> ``ConflictError(PLAN_ALREADY_ACTIVE)`` (409).
    """
    _reject_global(claims)
    plan_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO outreach_plans (tenant_id, plan_id, campaign_id, contact_id, plan, "
            "policy_notes, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            claims.tenant_id, plan_id, campaign_id, contact_id, plan, policy_notes,
            claims.subject,
        )
    except UniqueViolationError as exc:
        raise ConflictError("This contact already has an active outreach plan.",
                            code="PLAN_ALREADY_ACTIVE") from exc
    for task in tasks:
        await db.execute(
            "INSERT INTO outreach_tasks (tenant_id, task_id, plan_id, step_index, channel, goal, "
            "intent, scheduled_at, max_attempts, idempotency_key) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            claims.tenant_id, uuid4().hex, plan_id, task.step_index, task.channel, task.goal,
            task.intent, task.scheduled_at, task.max_attempts,
            idempotency_key(plan_id, task.step_index),
        )
    return plan_id


async def get_active_plan(
    db: Database, claims: AuthClaims, contact_id: str,
) -> dict[str, Any] | None:
    """The contact's active plan with its tasks, or ``None``."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT plan_id, campaign_id, contact_id, status, stop_reason, plan, policy_notes, "
        "created_by, created_at FROM outreach_plans "
        "WHERE tenant_id = $1 AND contact_id = $2 AND status = 'active'",
        claims.tenant_id, contact_id,
    )
    if row is None:
        return None
    plan = dict(row)
    plan["tasks"] = await list_plan_tasks(db, claims, plan["plan_id"])
    return plan


async def get_plan_contact_id(db: Database, claims: AuthClaims, plan_id: str) -> str | None:
    _reject_global(claims)
    value = await db.fetchval(
        "SELECT contact_id FROM outreach_plans WHERE tenant_id = $1 AND plan_id = $2",
        claims.tenant_id, plan_id,
    )
    return str(value) if value is not None else None


async def list_plan_tasks(db: Database, claims: AuthClaims, plan_id: str) -> list[TaskRow]:
    _reject_global(claims)
    rows = await db.fetch(
        _TASK_SELECT + "WHERE tenant_id = $1 AND plan_id = $2 ORDER BY step_index",
        claims.tenant_id, plan_id,
    )
    return [TaskRow(**dict(r)) for r in rows]


async def stop_plan(
    db: Database, claims: AuthClaims, plan_id: str, *, reason: str,
) -> int:
    """Stop an active plan and cancel its non-terminal tasks. Returns tasks cancelled
    (0 and no change when the plan isn't active)."""
    _reject_global(claims)
    async with db.acquire() as conn, conn.transaction():
        return await _stop_plan_in_tx(conn, claims, plan_id, reason=reason)


async def _stop_plan_in_tx(conn: Any, claims: AuthClaims, plan_id: str, *, reason: str) -> int:
    tag = await conn.execute(
        "UPDATE outreach_plans SET status = 'stopped', stop_reason = $3 "
        "WHERE tenant_id = $1 AND plan_id = $2 AND status = 'active'",
        claims.tenant_id, plan_id, reason,
    )
    if not str(tag).endswith(" 1"):
        return 0
    rows = await conn.fetch(
        "UPDATE outreach_tasks SET status = 'cancelled', last_error = $3 "
        "WHERE tenant_id = $1 AND plan_id = $2 AND status = ANY($4::text[]) RETURNING task_id",
        claims.tenant_id, plan_id, reason, list(_NON_TERMINAL),
    )
    return len(rows)


async def stop_contact_plan(
    db: Database, claims: AuthClaims, contact_id: str, *, reason: str,
) -> int:
    """Stop condition: stop the contact's active plan (if any). Returns tasks cancelled."""
    _reject_global(claims)
    plan_id = await db.fetchval(
        "SELECT plan_id FROM outreach_plans "
        "WHERE tenant_id = $1 AND contact_id = $2 AND status = 'active'",
        claims.tenant_id, contact_id,
    )
    if plan_id is None:
        return 0
    return await stop_plan(db, claims, str(plan_id), reason=reason)


@dataclass(frozen=True)
class TouchStats:
    touches: int
    last_touch_at: datetime | None
    call_attempts: int


async def touch_stats(
    db: Database, claims: AuthClaims, contact_id: str, *, now: datetime,
) -> TouchStats:
    """Completed touches (any channel, 365 days), the last one, and call attempts made."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT count(*) FILTER (WHERE t.status = 'completed') AS touches, "
        "max(t.completed_at) FILTER (WHERE t.status = 'completed') AS last_touch_at, "
        "coalesce(sum(t.attempts) FILTER (WHERE t.channel = 'call'), 0) AS call_attempts "
        "FROM outreach_tasks t JOIN outreach_plans p "
        "ON p.tenant_id = t.tenant_id AND p.plan_id = t.plan_id "
        "WHERE t.tenant_id = $1 AND p.contact_id = $2 "
        "AND coalesce(t.completed_at, t.scheduled_at) >= $3",
        claims.tenant_id, contact_id, now - timedelta(days=365),
    )
    if row is None:  # an aggregate always returns one row
        raise RuntimeError("touch_stats aggregate returned no row")
    return TouchStats(
        touches=int(row["touches"]),
        last_touch_at=row["last_touch_at"],
        call_attempts=int(row["call_attempts"]),
    )


# -- Task lifecycle (system-scoped claim, tenant-scoped transitions) ----------


@dataclass(frozen=True)
class ClaimedTask:
    tenant_id: str
    task_id: str
    plan_id: str
    contact_id: str
    channel: str
    goal: str
    intent: str
    attempts: int
    max_attempts: int
    step_index: int = 0  # A12 F4: the first step of an inbound plan is a first response
    plan_kind: str = ""


async def claim_due_tasks(
    db: Database, *, channels: list[str], limit: int, now: datetime,
) -> list[ClaimedTask]:
    """System-scoped, exactly-once claim of due ``scheduled`` tasks on registered
    channels (A1 D9), skipping tenants whose kill switch is on (the gate re-checks)."""
    if not channels:
        return []
    rows = await db.fetch(
        "UPDATE outreach_tasks t SET status = 'in_progress', claimed_at = now() "
        "FROM outreach_plans p "
        "WHERE p.tenant_id = t.tenant_id AND p.plan_id = t.plan_id "
        "AND (t.tenant_id, t.task_id) IN ("
        "  SELECT d.tenant_id, d.task_id FROM outreach_tasks d "
        "  WHERE d.status = 'scheduled' AND d.scheduled_at <= $1 AND d.channel = ANY($2::text[]) "
        "  AND NOT EXISTS (SELECT 1 FROM agent_settings s "
        "                  WHERE s.tenant_id = d.tenant_id AND s.outreach_paused) "
        "  ORDER BY d.scheduled_at FOR UPDATE OF d SKIP LOCKED LIMIT $3) "
        "RETURNING t.tenant_id, t.task_id, t.plan_id, p.contact_id, t.channel, t.goal, t.intent, "
        "t.attempts, t.max_attempts, t.step_index, coalesce(p.plan->>'kind', '') AS plan_kind",
        now, channels, limit,
    )
    return [ClaimedTask(**dict(r)) for r in rows]


async def mark_task_attempt(db: Database, claims: AuthClaims, task_id: str) -> int:
    """Count an execution attempt (only once the gate has passed). Returns attempts."""
    _reject_global(claims)
    value = await db.fetchval(
        "UPDATE outreach_tasks SET attempts = attempts + 1 "
        "WHERE tenant_id = $1 AND task_id = $2 RETURNING attempts",
        claims.tenant_id, task_id,
    )
    return int(value)


async def finish_task(
    db: Database,
    claims: AuthClaims,
    task_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Move an in-progress task -- or one parked in ``pending`` awaiting a human (A2 E8) --
    to its next state (completed/failed/cancelled/escalated, or ``pending`` to park it)."""
    _reject_global(claims)
    await db.execute(
        "UPDATE outreach_tasks SET status = $3, result = $4, last_error = $5, "
        "completed_at = CASE WHEN $3 = 'completed' THEN now() ELSE completed_at END "
        "WHERE tenant_id = $1 AND task_id = $2 AND status IN ('in_progress', 'pending')",
        claims.tenant_id, task_id, status, result, error,
    )


async def reschedule_task(
    db: Database, claims: AuthClaims, task_id: str, *, at: datetime, reason: str,
) -> None:
    """Back to ``scheduled`` at ``at`` (gate deferral or transient retry)."""
    _reject_global(claims)
    await db.execute(
        "UPDATE outreach_tasks SET status = 'scheduled', scheduled_at = $3, last_error = $4, "
        "claimed_at = NULL WHERE tenant_id = $1 AND task_id = $2 "
        "AND status IN ('in_progress', 'pending')",
        claims.tenant_id, task_id, at, reason,
    )


async def reap_stale_tasks_system(db: Database, *, lease_seconds: int) -> int:
    """``in_progress`` past the lease -> ``scheduled`` (``LEASE_EXPIRED``). System-scoped."""
    rows = await db.fetch(
        "UPDATE outreach_tasks SET status = 'scheduled', last_error = 'LEASE_EXPIRED', "
        "claimed_at = NULL WHERE status = 'in_progress' "
        "AND claimed_at < now() - make_interval(secs => $1) RETURNING task_id",
        lease_seconds,
    )
    return len(rows)


async def get_task(db: Database, claims: AuthClaims, task_id: str) -> dict[str, Any] | None:
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT task_id, plan_id, channel, status, attempts, max_attempts FROM outreach_tasks "
        "WHERE tenant_id = $1 AND task_id = $2", claims.tenant_id, task_id)
    return dict(row) if row is not None else None


async def refund_attempt(db: Database, claims: AuthClaims, task_id: str) -> None:
    """Give back an attempt the contact asked us to retry ("call me later", A6 L8)."""
    _reject_global(claims)
    await db.execute(
        "UPDATE outreach_tasks SET attempts = greatest(attempts - 1, 0) "
        "WHERE tenant_id = $1 AND task_id = $2", claims.tenant_id, task_id)


async def move_call_tasks(db: Database, claims: AuthClaims, plan_id: str, *, at: datetime) -> int:
    """Move the plan's next scheduled call to ``at`` (call me back, A7 N5). Returns 1/0."""
    _reject_global(claims)
    rows = await db.fetch(
        "UPDATE outreach_tasks SET scheduled_at = $3, last_error = 'CALLBACK_REQUESTED' "
        "WHERE (tenant_id, task_id) IN (SELECT tenant_id, task_id FROM outreach_tasks "
        "  WHERE tenant_id = $1 AND plan_id = $2 AND channel = 'call' AND status = 'scheduled' "
        "  ORDER BY scheduled_at LIMIT 1) RETURNING task_id",
        claims.tenant_id, plan_id, at)
    return len(rows)


async def bring_email_forward(db: Database, claims: AuthClaims, plan_id: str, *, at: datetime,
                              goal: str, intent: str) -> int:
    """Turn the plan's next scheduled email into ``goal``/``intent`` sent at ``at`` (the
    thank-you after a call). Returns 1/0."""
    _reject_global(claims)
    rows = await db.fetch(
        "UPDATE outreach_tasks SET scheduled_at = $3, goal = $4, intent = $5, "
        "last_error = 'AFTER_CALL' "
        "WHERE (tenant_id, task_id) IN (SELECT tenant_id, task_id FROM outreach_tasks "
        "  WHERE tenant_id = $1 AND plan_id = $2 AND channel = 'email' AND status = 'scheduled' "
        "  ORDER BY scheduled_at LIMIT 1) RETURNING task_id",
        claims.tenant_id, plan_id, at, goal, intent)
    return len(rows)



async def bring_call_forward(db: Database, claims: AuthClaims, contact_id: str, *,
                             at: datetime, intent: str) -> int:
    """Ring the contact's waiting ``intent`` call (the chat call) at ``at``. Returns 1/0."""
    _reject_global(claims)
    rows = await db.fetch(
        "UPDATE outreach_tasks SET scheduled_at = LEAST(scheduled_at, $3) "
        "WHERE (tenant_id, task_id) IN (SELECT t.tenant_id, t.task_id FROM outreach_tasks t "
        "  JOIN outreach_plans p ON p.tenant_id = t.tenant_id AND p.plan_id = t.plan_id "
        "  WHERE t.tenant_id = $1 AND p.contact_id = $2 AND p.status = 'active' "
        "  AND t.channel = 'call' AND t.status = 'scheduled' AND t.intent = $4) "
        "RETURNING task_id",
        claims.tenant_id, contact_id, at, intent)
    return len(rows)

# -- Console reads (A8) ------------------------------------------------------------------


async def get_latest_plan(
    db: Database, claims: AuthClaims, contact_id: str,
) -> dict[str, Any] | None:
    """The contact's active plan, else its most recent one, with tasks."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT plan_id, campaign_id, contact_id, status, stop_reason, plan, policy_notes, "
        "created_by, created_at FROM outreach_plans WHERE tenant_id = $1 AND contact_id = $2 "
        "ORDER BY status = 'active' DESC, created_at DESC LIMIT 1",
        claims.tenant_id, contact_id,
    )
    if row is None:
        return None
    plan = dict(row)
    plan["tasks"] = await list_plan_tasks(db, claims, plan["plan_id"])
    return plan


async def plan_counts(db: Database, claims: AuthClaims, *, now: datetime) -> dict[str, int]:
    """Active plans and scheduled tasks due within 24 hours."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT (SELECT count(*) FROM outreach_plans WHERE tenant_id = $1 "
        "        AND status = 'active') AS active_plans, "
        "       (SELECT count(*) FROM outreach_tasks WHERE tenant_id = $1 "
        "        AND status = 'scheduled' AND scheduled_at < $2) AS due",
        claims.tenant_id, now + timedelta(hours=24),
    )
    assert row is not None  # noqa: S101 -- scalar subqueries always return a row
    return {"active_plans": int(row["active_plans"]), "tasks_due_24h": int(row["due"])}


async def list_campaigns(
    db: Database, claims: AuthClaims, *, limit: int, offset: int,
) -> list[dict[str, Any]]:
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT c.campaign_id, c.name, c.kind, c.goal, c.status, c.created_by, c.created_at, "
        "c.ends_at, "
        "count(p.plan_id) FILTER (WHERE p.status = 'active') AS plans_active, "
        "count(p.plan_id) FILTER (WHERE p.status = 'stopped') AS plans_stopped, "
        "count(p.plan_id) FILTER (WHERE p.status = 'completed') AS plans_completed "
        "FROM outreach_campaigns c LEFT JOIN outreach_plans p "
        "ON p.tenant_id = c.tenant_id AND p.campaign_id = c.campaign_id "
        "WHERE c.tenant_id = $1 GROUP BY c.tenant_id, c.campaign_id "
        "ORDER BY c.created_at DESC LIMIT $2 OFFSET $3",
        claims.tenant_id, max(1, min(limit, 200)), max(0, offset),
    )
    return [dict(r) for r in rows]


async def stop_campaign(db: Database, claims: AuthClaims, campaign_id: str) -> int | None:
    """Stop every active plan in the campaign and mark it cancelled. Returns plans
    stopped, or ``None`` when the campaign isn't this tenant's."""
    _reject_global(claims)
    async with db.acquire() as conn, conn.transaction():
        tag = await conn.execute(
            "UPDATE outreach_campaigns SET status = 'cancelled' "
            "WHERE tenant_id = $1 AND campaign_id = $2",
            claims.tenant_id, campaign_id,
        )
        if not str(tag).endswith(" 1"):
            return None
        plan_ids = await conn.fetch(
            "SELECT plan_id FROM outreach_plans WHERE tenant_id = $1 AND campaign_id = $2 "
            "AND status = 'active' FOR UPDATE",
            claims.tenant_id, campaign_id,
        )
        for row in plan_ids:
            await _stop_plan_in_tx(conn, claims, row["plan_id"], reason="campaign_stopped")
        return len(plan_ids)


async def list_plans_for_contact(db: Database, claims: AuthClaims, contact_id: str
                                 ) -> list[dict[str, Any]]:
    """Every plan for the contact, newest first, with tasks (A9 H9 export)."""
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT plan_id, campaign_id, status, stop_reason, plan, policy_notes, created_by, "
        "created_at FROM outreach_plans WHERE tenant_id = $1 AND contact_id = $2 "
        "ORDER BY created_at DESC", claims.tenant_id, contact_id)
    plans = [dict(r) for r in rows]
    for plan in plans:
        plan["tasks"] = await list_plan_tasks(db, claims, plan["plan_id"])
    return plans


async def delete_plans_for_contact(db: Database, claims: AuthClaims, contact_id: str) -> int:
    """Delete the contact's plans (tasks cascade). Joins the caller's transaction."""
    _reject_global(claims)
    rows = await db.fetch("DELETE FROM outreach_plans WHERE tenant_id = $1 AND contact_id = $2 "
                          "RETURNING plan_id", claims.tenant_id, contact_id)
    return len(rows)


async def cancel_channel_tasks(db: Database, claims: AuthClaims, contact_id: str, channel: str,
                               *, reason: str) -> int:
    """Cancel the not-yet-run steps of ONE channel in the contact's active plan (A11/A12)."""
    _reject_global(claims)
    rows = await db.fetch(
        "UPDATE outreach_tasks t SET status = 'cancelled', last_error = $4 "
        "FROM outreach_plans p WHERE p.tenant_id = t.tenant_id AND p.plan_id = t.plan_id "
        "AND t.tenant_id = $1 AND p.contact_id = $2 AND p.status = 'active' "
        "AND t.channel = $3 AND t.status = 'scheduled' RETURNING t.task_id",
        claims.tenant_id, contact_id, channel, reason)
    return len(rows)


async def stop_plan_if_empty(db: Database, claims: AuthClaims, contact_id: str, *,
                             reason: str) -> bool:
    """Stop the contact's active plan when no step is left to run (A12 F3)."""
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE outreach_plans p SET status = 'stopped', stop_reason = $3 "
        "WHERE p.tenant_id = $1 AND p.contact_id = $2 AND p.status = 'active' "
        "AND NOT EXISTS (SELECT 1 FROM outreach_tasks t WHERE t.tenant_id = p.tenant_id "
        "  AND t.plan_id = p.plan_id AND t.status IN ('scheduled', 'pending', 'in_progress'))",
        claims.tenant_id, contact_id, reason)
    return str(tag).endswith(" 1")


async def latest_plan_ref(db: Database, claims: AuthClaims, contact_id: str
                          ) -> tuple[str, str] | None:
    """(campaign_id, plan kind) of the contact's most recent plan (A12 F1)."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT campaign_id, coalesce(plan->>'kind', 'outbound_manual') AS kind "
        "FROM outreach_plans WHERE tenant_id = $1 AND contact_id = $2 "
        "ORDER BY created_at DESC LIMIT 1", claims.tenant_id, contact_id)
    return (str(row["campaign_id"]), str(row["kind"])) if row is not None else None


async def set_campaign_end(db: Database, claims: AuthClaims, campaign_id: str,
                           ends_at: datetime | None) -> bool:
    """Set (or clear) a campaign's end date (A12 F5). ``False`` if not this tenant's."""
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE outreach_campaigns SET ends_at = $3 WHERE tenant_id = $1 AND campaign_id = $2",
        claims.tenant_id, campaign_id, ends_at)
    return str(tag).endswith(" 1")


async def end_campaigns_system(db: Database, *, now: datetime) -> int:
    """Housekeeping (Beat): campaigns past their end date are completed and their active
    plans stopped (``campaign_ended``). Returns plans stopped."""
    ended = await db.fetch(
        "UPDATE outreach_campaigns SET status = 'completed' "
        "WHERE status = 'active' AND ends_at IS NOT NULL AND ends_at <= $1 "
        "RETURNING tenant_id, campaign_id", now)
    stopped = 0
    for row in ended:
        async with db.acquire() as conn, conn.transaction():
            plans = await conn.fetch(
                "SELECT plan_id FROM outreach_plans WHERE tenant_id = $1 AND campaign_id = $2 "
                "AND status = 'active' FOR UPDATE", row["tenant_id"], row["campaign_id"])
            claims = AuthClaims(subject="system:agents", role=Role.CLIENT_ADMIN,
                                tenant_id=row["tenant_id"])
            for plan in plans:
                await _stop_plan_in_tx(conn, claims, plan["plan_id"], reason="campaign_ended")
                stopped += 1
    return stopped
