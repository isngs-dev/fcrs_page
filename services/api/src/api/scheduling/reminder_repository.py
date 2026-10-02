"""Reminder jobs repository -- tenant-scoped async SQL + the system-scoped claim (S8.3).

Every method except ``claim_due_reminders`` follows the ``api.scheduling.repository``
convention:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …).
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

``claim_due_reminders`` is the single documented exception (S8.3 decision 4):
Celery Beat has no tenant context, so it runs across all tenants with no
``claims`` argument and no tenant filter. Each row it returns carries its own
``tenant_id`` -- the caller (``api.scheduling.tasks.send_reminder``) re-scopes
to exactly that tenant before touching any tenant data.

Data model (migration 0020):
- ``reminder_jobs(job_id PK, tenant_id, event_id, "offset", run_at, status,
  attempts, last_error, created_at, updated_at)`` -- ``UNIQUE (tenant_id,
  event_id, "offset")`` backs idempotent creation; composite FK to
  ``schedule_events`` cascades deletes. ``"offset"`` is a reserved word --
  always quoted in SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.scheduling.reminders import OFFSETS


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Reminder jobs are always tenant-scoped (except the system-scoped
    ``claim_due_reminders``); a global caller has no tenant_id and therefore
    cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Reminder repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


@dataclass(frozen=True)
class ReminderJob:
    """A single tenant's reminder job row."""

    job_id: str
    event_id: str
    offset: str
    run_at: datetime
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ClaimedReminder:
    """A reminder job claimed by ``claim_due_reminders`` -- carries its own tenant_id.

    This is the only reminder-repository return type that carries
    ``tenant_id``: it is the payload the system-scoped dispatcher passes to
    ``send_reminder``, which re-scopes to exactly this tenant before any
    tenant-scoped read/write (S8.3 decision 4).
    """

    job_id: str
    tenant_id: str
    event_id: str
    offset: str


async def create_reminder_jobs(
    db: Database,
    claims: AuthClaims,
    *,
    event_id: str,
    starts_at: datetime,
    now: datetime,
) -> list[ReminderJob]:
    """Insert the 3 reminder rows for a newly-booked event, idempotently.

    One row per offset in ``OFFSETS`` (``3d``/``24h``/``1h``), ``run_at =
    starts_at - offset``. An offset whose ``run_at <= now`` is inserted with
    ``status='skipped'`` (never dispatched, deterministic, auditable) rather
    than omitted -- the row set for an event is always exactly 3 (S8.3
    decision 1). ``ON CONFLICT (tenant_id, event_id, "offset") DO NOTHING``
    makes a retried booking path idempotent -- it never duplicates rows.

    Returns the current (post-insert) set of 3 rows for the event, tenant-scoped.
    """
    _reject_global(claims)

    for offset, delta in OFFSETS.items():
        run_at = starts_at - delta
        status = "skipped" if run_at <= now else "pending"
        await db.execute(
            "INSERT INTO reminder_jobs "
            "(job_id, tenant_id, event_id, \"offset\", run_at, status) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (tenant_id, event_id, \"offset\") DO NOTHING",
            uuid4().hex,
            claims.tenant_id,
            event_id,
            offset,
            run_at,
            status,
        )

    rows = await db.fetch(
        "SELECT job_id, \"offset\", run_at, status, attempts, last_error, "
        "created_at, updated_at FROM reminder_jobs "
        "WHERE tenant_id = $1 AND event_id = $2 ORDER BY run_at",
        claims.tenant_id,
        event_id,
    )
    return [_row_to_reminder_job(row, event_id) for row in rows]


async def claim_due_reminders(db: Database, *, limit: int) -> list[ClaimedReminder]:
    """Atomically claim due, pending reminder rows across ALL tenants.

    System-scoped -- no ``claims`` argument (S8.3 decision 4): Celery Beat has
    no tenant context. The single ``UPDATE ... WHERE status = 'pending' ...
    RETURNING`` statement is the exactly-once gate -- a row transitions
    ``pending -> queued`` at most once, so two concurrent Beat ticks can never
    both claim the same job. ``FOR UPDATE ... SKIP LOCKED`` + ``LIMIT`` only
    reduce lock contention/tick cost; correctness does not depend on them. The
    JOIN on ``e.status = 'booked'`` means a future-cancelled event's reminders
    are never claimed even if a cancel path forgets to clean them up.
    """
    rows = await db.fetch(
        "UPDATE reminder_jobs SET status = 'queued', attempts = attempts + 1, updated_at = now() "
        "WHERE job_id IN ( "
        "    SELECT r.job_id FROM reminder_jobs r "
        "    JOIN schedule_events e ON e.tenant_id = r.tenant_id AND e.event_id = r.event_id "
        "    WHERE r.status = 'pending' AND r.run_at <= now() AND e.status = 'booked' "
        "    ORDER BY r.run_at "
        "    FOR UPDATE OF r SKIP LOCKED "
        "    LIMIT $1 "
        ") "
        "RETURNING job_id, tenant_id, event_id, \"offset\"",
        limit,
    )
    return [
        ClaimedReminder(
            job_id=str(row["job_id"]),
            tenant_id=str(row["tenant_id"]),
            event_id=str(row["event_id"]),
            offset=str(row["offset"]),
        )
        for row in rows
    ]


async def get_reminder_job(db: Database, claims: AuthClaims, job_id: str) -> ReminderJob | None:
    """Fetch a single tenant-scoped reminder job by id, or ``None`` if absent/foreign."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT job_id, event_id, \"offset\", run_at, status, attempts, last_error, "
        "created_at, updated_at FROM reminder_jobs WHERE tenant_id = $1 AND job_id = $2",
        claims.tenant_id,
        job_id,
    )
    if row is None:
        return None
    return _row_to_reminder_job(row, str(row["event_id"]))


async def mark_reminder(
    db: Database,
    claims: AuthClaims,
    job_id: str,
    *,
    status: str,
    last_error: str | None = None,
) -> None:
    """Update a tenant-scoped reminder job's ``status``/``last_error``.

    The ``WHERE`` clause filters by ``tenant_id`` so this can never touch
    another tenant's row even if ``job_id`` were guessed.
    """
    _reject_global(claims)

    await db.execute(
        "UPDATE reminder_jobs SET status = $3, last_error = $4, updated_at = now() "
        "WHERE tenant_id = $1 AND job_id = $2",
        claims.tenant_id,
        job_id,
        status,
        last_error,
    )


def _row_to_reminder_job(row: Any, event_id: str) -> ReminderJob:
    return ReminderJob(
        job_id=str(row["job_id"]),
        event_id=event_id,
        offset=str(row["offset"]),
        run_at=row["run_at"],
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"] if row["last_error"] is None else str(row["last_error"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def skip_pending_reminders(db: Database, claims: AuthClaims, event_id: str) -> None:
    """Mark a cancelled event's not-yet-sent reminders ``skipped`` (tenant-scoped).

    Belt and braces: ``claim_due_reminders`` already ignores cancelled events;
    this just makes the reminder rows themselves say so.
    """
    _reject_global(claims)
    await db.execute(
        "UPDATE reminder_jobs SET status = 'skipped', last_error = 'EVENT_CANCELLED', "
        "updated_at = now() WHERE tenant_id = $1 AND event_id = $2 AND status = 'pending'",
        claims.tenant_id,
        event_id,
    )
