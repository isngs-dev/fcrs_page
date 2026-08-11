"""Leads repository — tenant-scoped async SQL for visitor lead capture.

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …).
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

Data model (migration 0014):
- ``leads(tenant_id PK, lead_id PK, visitor_id, name, email, phone, status,
  stage, qualification_score, consent jsonb, assigned_agent_id, source,
  created_at, updated_at)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.leads.pipeline import STAGE_SORT_POSITION, STATUS_SORT_POSITION


@dataclass(frozen=True)
class Lead:
    """A single captured lead row."""

    lead_id: str
    visitor_id: str | None
    name: str | None
    email: str | None
    phone: str | None
    status: str
    stage: str
    qualification_score: int | None
    consent: dict[str, Any]
    assigned_agent_id: str | None
    source: str
    created_at: datetime
    updated_at: datetime
    converted_to_contact_id: str | None = None


@dataclass(frozen=True)
class LeadActivity:
    """A single append-only timeline event for a lead."""

    activity_id: str
    lead_id: str
    type: str
    payload: dict[str, Any] | None
    actor: str | None
    created_at: datetime


def _case_sort_sql(column: str, positions: dict[str, int]) -> tuple[str, str]:
    """Build closed CASE expressions for domain order plus unknown-last.

    Labels and positions are owned by the pure pipeline module at import time;
    neither comes from a request, even indirectly.
    """
    branches = " ".join(
        f"WHEN '{label}' THEN {int(position)}" for label, position in positions.items()
    )
    labels = ", ".join(f"'{label}'" for label in positions)
    return (
        f"(CASE {column} {branches} ELSE 99 END)",
        f"(CASE WHEN {column} IN ({labels}) THEN 0 ELSE 1 END)",
    )


# SR-25 D6: ORDER BY identifiers cannot be bound. The values in this dict are
# the *only* SQL fragments accepted for list ordering; caller values are used
# exclusively as dictionary keys and are never interpolated into SQL.
_STAGE_SORT_SQL, _STAGE_UNKNOWN_LAST_SQL = _case_sort_sql("stage", STAGE_SORT_POSITION)
_STATUS_SORT_SQL, _STATUS_UNKNOWN_LAST_SQL = _case_sort_sql("status", STATUS_SORT_POSITION)

_SORT_COLUMNS: dict[str, str] = {
    "name": "name",
    "email": "email",
    "stage": _STAGE_SORT_SQL,
    "status": _STATUS_SORT_SQL,
    "score": "qualification_score",
    "assigned": "assigned_agent_id",
    "created": "created_at",
}
_SORT_UNKNOWN_LAST: dict[str, str] = {
    "stage": _STAGE_UNKNOWN_LAST_SQL,
    "status": _STATUS_UNKNOWN_LAST_SQL,
}
LEAD_SORT_KEYS = frozenset(_SORT_COLUMNS)

_DEFAULT_SORT = "created"
_DEFAULT_DIRECTION = "desc"
LEAD_SORT_DEFAULT_DIRECTIONS: dict[str, str] = {
    "name": "asc",
    "email": "asc",
    "stage": "asc",
    "status": "asc",
    "score": "desc",
    "assigned": "asc",
    "created": "desc",
}


def _escape_ilike_pattern(value: str) -> str:
    """Escape user wildcard characters before binding a literal substring."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Lead capture is always tenant-scoped; a global caller has no tenant_id
    and therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Lead repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def create_lead(
    db: Database,
    claims: AuthClaims,
    *,
    visitor_id: str,
    name: str | None,
    email: str | None,
    phone: str | None,
    consent: dict[str, Any],
    source: str,
) -> str:
    """Insert a new ``leads`` row with ``status='new'``, ``stage='captured'``.

    Returns the ``lead_id`` (uuid4().hex). The ``consent`` dict is stored as
    jsonb (the default codec handles dict→jsonb conversion).

    ``name``/``email`` are nullable (SR-9.1 C4): an anonymous booking-created
    lead stores NULL for both rather than a fabricated placeholder -- honest
    empty contact info, never fake data. The ``leads.name``/``leads.email``
    columns must already be nullable (migration 0039) for this to persist
    cleanly against Postgres.
    """
    _reject_global(claims)

    new_lead_id = uuid4().hex
    params: list[Any] = [
        claims.tenant_id,
        new_lead_id,
        visitor_id,
        name,
        email,
        phone,
        "new",  # status (default)
        "captured",  # stage (default)
        None,  # qualification_score (NULL)
        consent,  # jsonb
        None,  # assigned_agent_id (NULL)
        source,
    ]
    await db.execute(
        "INSERT INTO leads "
        "(tenant_id, lead_id, visitor_id, name, email, phone, status, stage, "
        " qualification_score, consent, assigned_agent_id, source) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
        *params,
    )
    return new_lead_id


async def get_lead(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
) -> Lead | None:
    """Fetch a lead by ``lead_id`` scoped to the caller's tenant, or ``None``."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT lead_id, visitor_id, name, email, phone, status, stage, "
        "qualification_score, consent, assigned_agent_id, source, "
        "created_at, updated_at, converted_to_contact_id "
        "FROM leads "
        "WHERE tenant_id = $1 AND lead_id = $2",
        claims.tenant_id,
        lead_id,
    )
    return _row_to_lead(row) if row is not None else None


async def get_lead_email_by_visitor_id(
    db: Database,
    claims: AuthClaims,
    visitor_id: str,
) -> str | None:
    """Fetch the most-recent lead's email for a visitor, tenant-scoped.

    Used by ``api.notifications.recipients.resolve_event_recipient`` (S9.2,
    Scope §7) when a scheduled event has no ``lead_id`` but does have a
    ``visitor_id`` -- an anonymous booking that later (or earlier) captured a
    lead. Returns ``None`` if no lead exists for that visitor in this tenant.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT email FROM leads "
        "WHERE tenant_id = $1 AND visitor_id = $2 "
        "ORDER BY created_at DESC LIMIT 1",
        claims.tenant_id,
        visitor_id,
    )
    return str(row["email"]) if row is not None else None


async def get_lead_id_by_visitor_id(
    db: Database,
    claims: AuthClaims,
    visitor_id: str,
) -> str | None:
    """Fetch the most-recent lead's ``lead_id`` for a visitor, tenant-scoped.

    Mirrors ``get_lead_email_by_visitor_id`` (same tenant-scoped, newest-first
    shape). Used by ``api.scheduling.routes.book_slot`` (SR-9.1) to resolve
    create-or-link semantics: a visitor who already has a lead in this tenant
    gets it linked onto their booking; a visitor with none gets a new one
    created. Returns ``None`` if no lead exists for that visitor in this
    tenant.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT lead_id FROM leads "
        "WHERE tenant_id = $1 AND visitor_id = $2 "
        "ORDER BY created_at DESC LIMIT 1",
        claims.tenant_id,
        visitor_id,
    )
    return str(row["lead_id"]) if row is not None else None


async def update_lead_stage(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
    *,
    stage: str,
    status: str,
    qualification_score: int,
) -> Lead | None:
    """Update a lead's ``stage``/``status``/``qualification_score``, tenant-scoped.

    ``status`` and ``qualification_score`` must already be derived (see
    ``api.leads.pipeline``) -- this method persists them as-is; it does not
    validate the transition itself.

    Returns the updated ``Lead``, or ``None`` if no row matched (missing
    ``lead_id`` or cross-tenant access -- callers cannot distinguish the two,
    which is the point: no cross-tenant existence leak).
    """
    _reject_global(claims)

    result = await db.execute(
        "UPDATE leads SET stage = $1, status = $2, qualification_score = $3, "
        "updated_at = now() "
        "WHERE tenant_id = $4 AND lead_id = $5",
        stage,
        status,
        qualification_score,
        claims.tenant_id,
        lead_id,
    )
    if _rows_affected(result) == 0:
        return None

    return await get_lead(db, claims, lead_id)


async def update_lead_contact(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
    *,
    name: str | None,
    email: str | None,
    consent: dict[str, Any],
) -> bool:
    """Fill in name/email/consent on an EXISTING lead, tenant-scoped (SR-14 D6).

    The first repository method that mutates ``leads.name``/``leads.email``
    (every prior ``UPDATE leads`` statement -- ``update_lead_stage``,
    ``assign_lead``, ``mark_lead_converted`` -- touches only stage/assignment/
    conversion). Needed for the conversation-start identity gate's "link"
    branch (D5): a visitor who already has a lead in this tenant (e.g. an
    anonymous SR-9.1 booking that left ``name``/``email`` NULL, migration
    0039) gets their captured identity written onto that SAME row rather than
    a duplicate lead being created.

    ``COALESCE($1, name)``/``COALESCE($2, email)`` guard against overwriting
    good data with nothing: a ``None`` argument here keeps whatever the row
    already had, it never nulls out an existing value. In practice the
    identity-gate endpoint always supplies non-null ``name``/``email``
    (both are required fields), so this guard is defense-in-depth, not the
    primary path.

    Returns ``True`` if a row was updated (tenant + lead_id matched),
    ``False`` otherwise (missing ``lead_id`` or cross-tenant access --
    callers cannot distinguish the two, matching ``update_lead_stage``'s
    no-cross-tenant-existence-leak contract). Raises ``ValidationError`` for
    global (PLATFORM_ADMIN) callers.
    """
    _reject_global(claims)

    result = await db.execute(
        "UPDATE leads SET name = COALESCE($1, name), email = COALESCE($2, email), "
        "consent = $3, updated_at = now() "
        "WHERE tenant_id = $4 AND lead_id = $5",
        name,
        email,
        consent,
        claims.tenant_id,
        lead_id,
    )
    return _rows_affected(result) > 0


async def add_activity(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
    *,
    type: str,
    payload: dict[str, Any] | None,
    actor: str,
) -> str:
    """Append a timeline event for a lead. Returns the new ``activity_id``.

    Tenant-scoped INSERT; ``payload`` is bound as jsonb (default codec).
    This does not verify the lead exists -- callers (routes) are expected to
    have already resolved the lead via ``get_lead`` before appending.
    """
    _reject_global(claims)

    new_activity_id = uuid4().hex
    await db.execute(
        "INSERT INTO lead_activities "
        "(tenant_id, activity_id, lead_id, type, payload, actor) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        claims.tenant_id,
        new_activity_id,
        lead_id,
        type,
        payload,
        actor,
    )
    return new_activity_id


async def list_activities(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
) -> list[LeadActivity]:
    """Fetch a lead's timeline, tenant-scoped, newest first."""
    _reject_global(claims)

    rows = await db.fetch(
        "SELECT activity_id, lead_id, type, payload, actor, created_at "
        "FROM lead_activities "
        "WHERE tenant_id = $1 AND lead_id = $2 "
        "ORDER BY created_at DESC",
        claims.tenant_id,
        lead_id,
    )
    return [_row_to_activity(row) for row in rows]


async def assign_lead(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
    *,
    agent_id: str,
) -> Lead | None:
    """Assign a lead to an agent, tenant-scoped.

    Persists ``assigned_agent_id`` as-is -- callers (routes) are expected to
    have already validated the assignee via ``auth.repository.get_user_by_id``
    before calling this. Returns the updated ``Lead``, or ``None`` if no row
    matched (missing ``lead_id`` or cross-tenant access).
    """
    _reject_global(claims)

    result = await db.execute(
        "UPDATE leads SET assigned_agent_id = $1, updated_at = now() "
        "WHERE tenant_id = $2 AND lead_id = $3",
        agent_id,
        claims.tenant_id,
        lead_id,
    )
    if _rows_affected(result) == 0:
        return None

    return await get_lead(db, claims, lead_id)


async def list_leads_for_export(
    db: Database,
    claims: AuthClaims,
) -> list[Lead]:
    """Fetch all of the caller's tenant leads, newest first, for CSV export.

    Tenant-scoped (S7.4 decision 5) -- other tenants' leads are never
    returned. Raises ``ValidationError`` for global callers (PLATFORM_ADMIN).
    """
    _reject_global(claims)

    rows = await db.fetch(
        "SELECT lead_id, visitor_id, name, email, phone, status, stage, "
        "qualification_score, consent, assigned_agent_id, source, "
        "created_at, updated_at, converted_to_contact_id "
        "FROM leads "
        "WHERE tenant_id = $1 "
        "ORDER BY created_at DESC",
        claims.tenant_id,
    )
    return [_row_to_lead(row) for row in rows]


async def list_leads(
    db: Database,
    claims: AuthClaims,
    *,
    limit: int = 50,
    offset: int = 0,
    stage: str | None = None,
    status: str | None = None,
    assigned_agent_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    include_converted: bool = False,
    q: str | None = None,
    sort: str = _DEFAULT_SORT,
    direction: str = _DEFAULT_DIRECTION,
) -> tuple[list[Lead], int]:
    """Fetch a paginated, filtered page of the caller's tenant leads.

    Tenant-scoped (``WHERE tenant_id = $1``); each supplied filter appends
    exactly one positional ``AND`` clause, values always bound (never
    interpolated). The route validates filter values; ``sort`` and
    ``direction`` are additionally validated here because ORDER BY structure
    cannot be bound. ``created_from``/``created_to`` filter ``created_at``
    as a half-open ``[from, to)`` window. ``q`` is a literal,
    case-insensitive Name-or-Email substring match.

    ``include_converted`` (SR-9.2 D1): when ``False`` (the default), a lead
    that has been converted to a Contact (``converted_to_contact_id IS NOT
    NULL``) is excluded from the page -- the tombstone stays out of the
    working list by default. Pass ``True`` to see conversion history.

    Returns ``(rows, total)`` -- ``total`` is a ``count(*)`` over the same
    filtered WHERE (minus LIMIT/OFFSET), ordered by the SR-25 closed
    allowlist with ``lead_id DESC`` as the mandatory total-order tie-break.
    """
    _reject_global(claims)

    if sort not in _SORT_COLUMNS:
        raise ValidationError(f"Unknown sort key {sort!r}.", code="INVALID_LEAD_SORT")
    if direction not in {"asc", "desc"}:
        raise ValidationError(
            f"Unknown sort direction {direction!r}.", code="INVALID_LEAD_SORT_DIRECTION",
        )

    where = "WHERE tenant_id = $1"
    params: list[Any] = [claims.tenant_id]

    if stage is not None:
        params.append(stage)
        where += f" AND stage = ${len(params)}"
    if status is not None:
        params.append(status)
        where += f" AND status = ${len(params)}"
    if assigned_agent_id is not None:
        params.append(assigned_agent_id)
        where += f" AND assigned_agent_id = ${len(params)}"
    if created_from is not None:
        params.append(created_from)
        where += f" AND created_at >= ${len(params)}"
    if created_to is not None:
        params.append(created_to)
        where += f" AND created_at < ${len(params)}"
    if q:
        escaped_q = _escape_ilike_pattern(q)
        params.extend([f"%{escaped_q}%", f"%{escaped_q}%"])
        name_idx = len(params) - 1
        email_idx = len(params)
        where += (
            f" AND (name ILIKE ${name_idx} ESCAPE '\\' "
            f"OR email ILIKE ${email_idx} ESCAPE '\\')"
        )
    if not include_converted:
        where += " AND converted_to_contact_id IS NULL"

    sort_expr = _SORT_COLUMNS[sort]
    direction_sql = "ASC" if direction == "asc" else "DESC"
    # sort_expr comes only from the developer-authored dict above; the
    # request's sort/direction strings are never interpolated.
    unknown_last_expr = _SORT_UNKNOWN_LAST.get(sort)
    unknown_last_prefix = f"{unknown_last_expr} ASC, " if unknown_last_expr else ""
    order_by = (
        f"ORDER BY {unknown_last_prefix}{sort_expr} {direction_sql} NULLS LAST, lead_id DESC"
    )

    # Parameterized SQL; `where` is a safe constant clause built above and
    # order_by is constrained to the developer-authored allowlist above.
    # ruff: noqa: S608
    count_row = await db.fetchrow(
        "SELECT count(*) AS count FROM leads " + where, *params,
    )
    total = int(count_row["count"]) if count_row is not None else 0

    clamped_limit = max(1, min(limit, 200))
    page_params = [*params, clamped_limit, max(0, offset)]
    limit_idx = len(page_params) - 1
    offset_idx = len(page_params)
    rows = await db.fetch(
        "SELECT lead_id, visitor_id, name, email, phone, status, stage, "
        "qualification_score, consent, assigned_agent_id, source, "
        "created_at, updated_at, converted_to_contact_id "
        "FROM leads " + where + " "
        f"{order_by} LIMIT ${limit_idx} OFFSET ${offset_idx}",
        *page_params,
    )
    return [_row_to_lead(row) for row in rows], total


async def list_lead_ids_converted_to_contact(
    db: Database,
    claims: AuthClaims,
    contact_id: str,
) -> list[str]:
    """Fetch ``lead_id``s whose ``converted_to_contact_id`` equals ``contact_id``.

    Used by the SR-9.3 timeline's identity resolution (D2) as the
    belt-and-braces complement to ``contacts.lead_id``: a Contact's identity
    set includes both its own originating lead (if any) AND any lead whose
    conversion points at it. SR-9.2 D6's partial unique index on
    ``contacts(tenant_id, lead_id)`` means this is at most one in practice,
    but this query does not assume that -- it returns a list. Tenant-scoped;
    raises ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    rows = await db.fetch(
        "SELECT lead_id FROM leads "
        "WHERE tenant_id = $1 AND converted_to_contact_id = $2",
        claims.tenant_id,
        contact_id,
    )
    return [str(row["lead_id"]) for row in rows]


async def mark_lead_converted(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
    *,
    contact_id: str,
) -> bool:
    """Tombstone a lead on conversion (SR-9.2 D1/D5/D6).

    Sets ``stage='converted'``, ``status='won'``, and
    ``converted_to_contact_id=contact_id`` -- directly, bypassing
    ``api.leads.pipeline.validate_transition`` entirely (D5): conversion is
    a lifecycle action, not a funnel step, and must succeed even from
    ``stage='captured'`` where the pipeline validator would reject it.

    The UPDATE is guarded by ``AND converted_to_contact_id IS NULL`` -- a
    second, independent idempotency backstop beneath the route's primary
    ``contacts(tenant_id, lead_id)`` partial-unique-index guard (D6): a lead
    that is already converted matches zero rows here.

    Does NOT touch ``lead_activities`` -- the Lead row is never deleted
    (K5/D1); its timeline is untouched by this call.

    Returns ``True`` if a row was updated (first, successful conversion),
    ``False`` if no row matched (missing/cross-tenant lead_id, OR the lead
    was already converted).
    """
    _reject_global(claims)

    result = await db.execute(
        "UPDATE leads SET stage = 'converted', status = 'won', "
        "converted_to_contact_id = $1, updated_at = now() "
        "WHERE tenant_id = $2 AND lead_id = $3 AND converted_to_contact_id IS NULL",
        contact_id,
        claims.tenant_id,
        lead_id,
    )
    return _rows_affected(result) > 0


def _rows_affected(command_tag: str) -> int:
    """Parse the row count from an asyncpg-style command tag (e.g. 'UPDATE 1')."""
    parts = command_tag.strip().split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_lead(row: Any) -> Lead:
    return Lead(
        lead_id=str(row["lead_id"]),
        visitor_id=row["visitor_id"],
        name=row["name"] if row["name"] is None else str(row["name"]),
        email=row["email"] if row["email"] is None else str(row["email"]),
        phone=row["phone"],
        status=str(row["status"]),
        stage=str(row["stage"]),
        qualification_score=row["qualification_score"],
        consent=row["consent"],
        assigned_agent_id=row["assigned_agent_id"],
        source=str(row["source"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        converted_to_contact_id=(
            row["converted_to_contact_id"] if "converted_to_contact_id" in row.keys() else None
        ),
    )


def _row_to_activity(row: Any) -> LeadActivity:
    return LeadActivity(
        activity_id=str(row["activity_id"]),
        lead_id=str(row["lead_id"]),
        type=str(row["type"]),
        payload=row["payload"],
        actor=row["actor"],
        created_at=row["created_at"],
    )
