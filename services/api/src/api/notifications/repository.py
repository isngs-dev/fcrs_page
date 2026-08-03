"""Notification jobs repository -- tenant-scoped async SQL (S9.1).

Every method takes ``AuthClaims`` as its first positional argument, calls
``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope), and
uses positional placeholders (``$1``, ``$2``, ...) -- mirrors
``api.scheduling.reminder_repository``.

This module never imports Celery -- the ``.delay()`` call lives in the route/
task layer, not the repo (same seam as ``api.leads.routes`` -> ``crm.sync_lead``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Notification jobs are always tenant-scoped; a global caller has no
    tenant_id and therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Notification job repository is tenant-scoped; PLATFORM_ADMIN "
            "callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


@dataclass(frozen=True)
class NotificationJob:
    """A single tenant's notification job row."""

    job_id: str
    channel: str
    template: str | None
    recipient: str
    subject: str
    body: str
    payload: dict[str, Any] | None
    dedupe_key: str
    status: str
    attempts: int
    delivery_ref: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    lead_id: str | None = None


async def enqueue_notification(
    db: Database,
    claims: AuthClaims,
    *,
    channel: str,
    recipient: str,
    subject: str,
    body: str,
    dedupe_key: str,
    template: str | None = None,
    payload: dict[str, Any] | None = None,
    lead_id: str | None = None,
) -> str | None:
    """Idempotently enqueue a notification job on ``(tenant_id, dedupe_key)``.

    A new row -> returns its ``job_id`` (caller ``.delay()``s the send). A
    conflict (same ``dedupe_key`` already enqueued for this tenant) -> the
    ``RETURNING`` clause matches no rows -> returns ``None`` and the caller
    does NOT enqueue a duplicate send (S9.1 decision 4).

    ``lead_id`` (SR-9.3 D4): optional, defaults to ``None`` so existing call
    sites (password reset, admin test-send, Calendly webhook) are unaffected.
    Only customer-facing sites with a lead already in scope
    (``notifications/reminder_sink.py``, ``scheduling/routes.py``) pass it.
    No composite FK is possible here (J4) -- this is a plain nullable column,
    correctness rests on the query predicate in ``list_jobs_for_leads``.

    Raises ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    job_id = uuid4().hex
    result = await db.fetchval(
        "INSERT INTO notification_jobs "
        "(job_id, tenant_id, channel, template, recipient, subject, body, payload, "
        " dedupe_key, lead_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
        "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING "
        "RETURNING job_id",
        job_id,
        claims.tenant_id,
        channel,
        template,
        recipient,
        subject,
        body,
        payload,
        dedupe_key,
        lead_id,
    )
    return str(result) if result is not None else None


async def list_jobs_for_leads(
    db: Database,
    claims: AuthClaims,
    *,
    lead_ids: list[str],
    before: datetime | None = None,
    limit: int = 50,
) -> list[NotificationJob]:
    """Fetch tenant-scoped notification jobs attributed to any of ``lead_ids``.

    Used by the SR-9.3 timeline fan-out. ``WHERE tenant_id = $1 AND
    lead_id = ANY($2)`` -- this is the ONE join in the timeline sprint that
    rests purely on a query predicate rather than a composite FK (J4/D4): a
    ``lead_id`` string colliding across two tenants must never leak a job
    from the wrong tenant, and that guarantee lives entirely in this
    ``tenant_id = $1`` clause, not in the schema. Newest-first
    (``created_at DESC``). Returns ``[]`` for an empty ``lead_ids`` list
    (no query issued). Raises ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    if not lead_ids:
        return []

    params: list[Any] = [claims.tenant_id, lead_ids]
    where = "WHERE tenant_id = $1 AND lead_id = ANY($2)"

    if before is not None:
        params.append(before)
        where += f" AND created_at < ${len(params)}"

    params.append(max(1, min(limit, 200)))

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    rows = await db.fetch(
        "SELECT job_id, channel, template, recipient, subject, body, payload, "
        "dedupe_key, status, attempts, delivery_ref, last_error, created_at, "
        "updated_at, lead_id "
        "FROM notification_jobs " + where + " "
        f"ORDER BY created_at DESC LIMIT ${len(params)}",
        *params,
    )
    return [_row_to_job(row) for row in rows]


async def get_notification_job_id_by_dedupe_key(
    db: Database, claims: AuthClaims, dedupe_key: str
) -> str | None:
    """Look up an existing tenant-scoped job's id by its dedupe key.

    Used by the test-send route to return a stable ``job_id`` when
    ``enqueue_notification`` reports a conflict (already enqueued).
    """
    _reject_global(claims)

    result = await db.fetchval(
        "SELECT job_id FROM notification_jobs WHERE tenant_id = $1 AND dedupe_key = $2",
        claims.tenant_id,
        dedupe_key,
    )
    return str(result) if result is not None else None


async def get_notification_job(
    db: Database, claims: AuthClaims, job_id: str
) -> NotificationJob | None:
    """Fetch a single tenant-scoped notification job by id, or ``None`` if absent/foreign."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT job_id, channel, template, recipient, subject, body, payload, dedupe_key, "
        "status, attempts, delivery_ref, last_error, created_at, updated_at "
        "FROM notification_jobs WHERE tenant_id = $1 AND job_id = $2",
        claims.tenant_id,
        job_id,
    )
    if row is None:
        return None
    return _row_to_job(row)


async def mark_notification(
    db: Database,
    claims: AuthClaims,
    job_id: str,
    *,
    status: str,
    delivery_ref: str | None = None,
    last_error: str | None = None,
    increment_attempt: bool = False,
) -> None:
    """Update a tenant-scoped notification job's status/delivery_ref/last_error.

    ``attempts`` is incremented only when ``increment_attempt=True`` -- once
    per REAL send attempt (S9.1 decision 5): a config error (no config /
    disabled / unknown provider / missing SMTP fields) is caught before any
    network call and does NOT count as an attempt; a provider ``send()`` call
    (success or deterministic provider failure) does. The ``WHERE`` clause
    filters by ``tenant_id`` so this can never touch another tenant's row
    even if ``job_id`` were guessed. When ``status='sent'`` the update is
    additionally guarded by ``status='pending'`` -- the exactly-once flip
    (S9.1 decision 5e): if a concurrent delivery already flipped the row,
    this ``UPDATE`` matches 0 rows and no second flip happens.
    """
    _reject_global(claims)

    if status == "sent":
        if increment_attempt:
            query = (
                "UPDATE notification_jobs SET status = $3, delivery_ref = $4, "
                "attempts = attempts + 1, updated_at = now() "
                "WHERE tenant_id = $1 AND job_id = $2 AND status = 'pending'"
            )
        else:
            query = (
                "UPDATE notification_jobs SET status = $3, delivery_ref = $4, "
                "updated_at = now() "
                "WHERE tenant_id = $1 AND job_id = $2 AND status = 'pending'"
            )
        await db.execute(query, claims.tenant_id, job_id, status, delivery_ref)
        return

    if increment_attempt:
        query = (
            "UPDATE notification_jobs SET status = $3, last_error = $4, "
            "attempts = attempts + 1, updated_at = now() "
            "WHERE tenant_id = $1 AND job_id = $2"
        )
    else:
        query = (
            "UPDATE notification_jobs SET status = $3, last_error = $4, "
            "updated_at = now() "
            "WHERE tenant_id = $1 AND job_id = $2"
        )
    await db.execute(query, claims.tenant_id, job_id, status, last_error)


def _row_to_job(row: Any) -> NotificationJob:
    return NotificationJob(
        job_id=str(row["job_id"]),
        channel=str(row["channel"]),
        template=row["template"] if row["template"] is None else str(row["template"]),
        recipient=str(row["recipient"]),
        subject=str(row["subject"]),
        body=str(row["body"]),
        payload=row["payload"],
        dedupe_key=str(row["dedupe_key"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        delivery_ref=(
            row["delivery_ref"] if row["delivery_ref"] is None else str(row["delivery_ref"])
        ),
        last_error=row["last_error"] if row["last_error"] is None else str(row["last_error"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        lead_id=(
            row["lead_id"]
            if "lead_id" in row.keys() and row["lead_id"] is not None
            else None
        ),
    )
