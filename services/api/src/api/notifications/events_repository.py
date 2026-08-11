"""Notification events repository -- tenant-scoped async SQL for the
in-console notifications feed (SR-21).

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …).
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

D1: this is DELIBERATELY NOT the outbound ``notification_jobs`` ledger
(``api.notifications.repository``) -- different table, different direction,
different lifecycle. Nothing here has a recipient/subject/body/channel/
delivery status, and this module never reads or writes ``notification_jobs``.

D6: read/unread state is PER-USER (``notification_event_reads``), never
shared across users in the same tenant -- the whole point of this sprint's
data model. ``list_events`` resolves each row's ``read`` flag against the
CALLING user's rows only.

Data model (migration 0044):
- ``notification_events(tenant_id PK, event_id PK, kind, category,
  target_type, target_id, payload jsonb, actor_id, created_at)``.
- ``notification_event_reads(tenant_id PK, event_id PK, user_id PK, read_at)``
  with a composite FK back to ``notification_events`` -- a cross-tenant read
  row is unrepresentable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


@dataclass(frozen=True)
class NotificationEvent:
    """A single feed row, with the CALLING user's read state resolved."""

    event_id: str
    kind: str
    category: str
    target_type: str | None
    target_id: str | None
    payload: dict[str, Any] | None
    actor_id: str | None
    created_at: datetime
    read: bool


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    The feed is always tenant-scoped; a global caller has no tenant_id and
    therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Notification events repository is tenant-scoped; "
            "PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def emit_event(
    db: Database,
    claims: AuthClaims,
    *,
    kind: str,
    category: str,
    target_type: str | None,
    target_id: str | None,
    payload: dict[str, Any] | None,
    actor_id: str | None,
) -> str:
    """Insert a new ``notification_events`` row. Returns the ``event_id``.

    ``payload`` must be PII-free by construction (ids only -- never a name,
    email, phone or message content); this is enforced by every emit call
    site, not by this function. Callers should go through
    ``api.notifications.emit.emit_event_safe`` rather than calling this
    directly, so the fail-open guarantee (D2) cannot be forgotten.
    """
    _reject_global(claims)

    new_event_id = uuid4().hex
    now = datetime.now(UTC)
    await db.execute(
        "INSERT INTO notification_events "
        "(tenant_id, event_id, kind, category, target_type, target_id, "
        " payload, actor_id, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        claims.tenant_id,
        new_event_id,
        kind,
        category,
        target_type,
        target_id,
        payload,
        actor_id,
        now,
    )
    return new_event_id


async def list_events(
    db: Database,
    claims: AuthClaims,
    *,
    user_id: str,
    category: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[NotificationEvent], int, int]:
    """Fetch a page of the caller's tenant's events, newest first.

    Returns ``(rows, total, unread_count)`` -- the house ``(rows, total)``
    idiom (mirrors ``contacts.list_contacts``) plus ``unread_count``, all
    resolved against ``user_id`` (the CALLING user, never a request body).

    ``limit`` is clamped server-side to ``[1, 200]`` (D7, mirrors
    ``accounts``' clamp). ``category``/``unread_only`` filter but never
    widen scope beyond the caller's tenant.

    Parameterized SQL; every dynamically-appended clause below uses ``$N``
    placeholders only, never string-interpolated values (mirrors
    ``analytics/reports_repository.py``'s identical pattern).
    """
    _reject_global(claims)

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)

    params: list[Any] = [claims.tenant_id]
    where_extra = ""
    if category is not None:
        params.append(category)
        where_extra += f" AND category = ${len(params)}"

    params.append(user_id)
    user_id_param_idx = len(params)

    unread_clause = ""
    if unread_only:
        unread_clause = (
            f" AND NOT EXISTS (SELECT 1 FROM notification_event_reads r "
            f"WHERE r.tenant_id = notification_events.tenant_id "
            f"AND r.event_id = notification_events.event_id "
            f"AND r.user_id = ${user_id_param_idx})"
        )

    params.append(clamped_limit)
    params.append(clamped_offset)

    # Parameterized SQL; `where_extra`/`unread_clause` are safe constant
    # clauses built above using only `$N` placeholders, never
    # string-interpolated values (mirrors analytics/reports_repository.py).
    # ruff: noqa: S608
    rows = await db.fetch(
        "SELECT event_id, kind, category, target_type, target_id, payload, "
        "actor_id, created_at, "
        f"EXISTS (SELECT 1 FROM notification_event_reads r WHERE "
        f"r.tenant_id = notification_events.tenant_id AND "
        f"r.event_id = notification_events.event_id AND "
        f"r.user_id = ${user_id_param_idx}) AS read "
        "FROM notification_events "
        f"WHERE tenant_id = $1{where_extra}{unread_clause} "
        "ORDER BY created_at DESC, event_id DESC "
        f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
        *params,
    )

    count_params: list[Any] = [claims.tenant_id]
    count_extra = ""
    if category is not None:
        count_params.append(category)
        count_extra = f" AND category = ${len(count_params)}"
    count_unread_extra = ""
    if unread_only:
        count_params.append(user_id)
        count_unread_extra = (
            f" AND NOT EXISTS (SELECT 1 FROM notification_event_reads r "
            f"WHERE r.tenant_id = notification_events.tenant_id "
            f"AND r.event_id = notification_events.event_id "
            f"AND r.user_id = ${len(count_params)})"
        )
    # Parameterized SQL; `count_extra`/`count_unread_extra` use only `$N`
    # placeholders. ruff: noqa: S608
    total = await db.fetchval(
        "SELECT COUNT(*) FROM notification_events "
        f"WHERE tenant_id = $1{count_extra}{count_unread_extra}",
        *count_params,
    )

    unread_count = await db.fetchval(
        "SELECT COUNT(*) FROM notification_events e WHERE e.tenant_id = $1 "
        "AND NOT EXISTS (SELECT 1 FROM notification_event_reads r "
        "WHERE r.tenant_id = e.tenant_id AND r.event_id = e.event_id "
        "AND r.user_id = $2)",
        claims.tenant_id,
        user_id,
    )

    return [_row_to_event(row) for row in rows], int(total or 0), int(unread_count or 0)


async def mark_read(
    db: Database,
    claims: AuthClaims,
    *,
    user_id: str,
    event_id: str,
    read: bool,
) -> bool:
    """Set/clear the CALLING user's read state for one event.

    Returns ``True`` if the event exists in the caller's tenant (regardless
    of whether the read row already existed -- idempotent), ``False`` if the
    event does not exist or belongs to another tenant (the caller's route
    layer turns this into a 404, no existence leak). Never affects any other
    user's read state (D6).
    """
    _reject_global(claims)

    existing = await db.fetchrow(
        "SELECT event_id FROM notification_events WHERE tenant_id = $1 AND event_id = $2",
        claims.tenant_id,
        event_id,
    )
    if existing is None:
        return False

    if read:
        await db.execute(
            "INSERT INTO notification_event_reads (tenant_id, event_id, user_id, read_at) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (tenant_id, event_id, user_id) DO NOTHING",
            claims.tenant_id,
            event_id,
            user_id,
            datetime.now(UTC),
        )
    else:
        await db.execute(
            "DELETE FROM notification_event_reads "
            "WHERE tenant_id = $1 AND event_id = $2 AND user_id = $3",
            claims.tenant_id,
            event_id,
            user_id,
        )
    return True


async def mark_all_read(
    db: Database,
    claims: AuthClaims,
    *,
    user_id: str,
    category: str | None = None,
) -> int:
    """Mark every (optionally category-filtered) unread event in the
    caller's tenant as read, for the CALLING user only. Returns the count
    of events newly marked."""
    _reject_global(claims)

    rows, _, _ = await list_events(
        db, claims, user_id=user_id, category=category, unread_only=True, limit=200, offset=0,
    )
    marked = 0
    for row in rows:
        ok = await mark_read(db, claims, user_id=user_id, event_id=row.event_id, read=True)
        if ok:
            marked += 1
    return marked


async def prune_events_older_than(db: Database, *, retention_days: int) -> int:
    """Delete ``notification_events`` rows older than ``retention_days`` (D7).

    Cross-tenant by design (this is a periodic maintenance task, not a
    caller-facing operation) -- it takes no ``AuthClaims`` and is never
    reachable from an HTTP route. ``notification_event_reads`` rows for the
    deleted events are removed automatically via the composite
    ``ON DELETE CASCADE`` foreign key (the migration's deliberate, documented
    cascade -- a read-state row has no meaning without its event).

    Idempotent: running this twice back-to-back deletes nothing extra on the
    second run (the first run already removed everything past the window).
    Returns the number of ``notification_events`` rows deleted.
    """
    deleted = await db.fetch(
        "DELETE FROM notification_events "
        "WHERE created_at < now() - make_interval(days => $1) "
        "RETURNING event_id",
        int(retention_days),
    )
    return len(deleted)


def _row_to_event(row: Any) -> NotificationEvent:
    return NotificationEvent(
        event_id=str(row["event_id"]),
        kind=str(row["kind"]),
        category=str(row["category"]),
        target_type=row["target_type"],
        target_id=row["target_id"],
        payload=row["payload"],
        actor_id=row["actor_id"],
        created_at=row["created_at"],
        read=bool(row["read"]),
    )
