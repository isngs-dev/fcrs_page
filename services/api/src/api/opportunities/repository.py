"""Opportunities repository -- tenant-scoped async SQL for the Opportunity
(deal) entity.

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …).
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

Data model (migration 0042):
- ``opportunities(tenant_id PK, opportunity_id PK, contact_id NOT NULL,
  account_id?, name, amount numeric(14,2)?, currency NOT NULL, stage,
  expected_close_date?, closed_at?, close_reason?, owner_agent_id?,
  created_at, updated_at)``. No ``win_probability`` column -- it is
  derived at read time by ``opportunities/pipeline.win_probability_for_stage``
  (D2), never stored here.

Money (D7): ``amount`` crosses this module as ``decimal.Decimal | None``,
never ``float``. asyncpg reads/writes ``NUMERIC`` as ``Decimal`` natively.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


@dataclass(frozen=True)
class Opportunity:
    """A single opportunity (deal) row. No ``win_probability`` field -- it
    is derived by callers via ``opportunities.pipeline.win_probability_for_stage``,
    never stored or returned by the repository."""

    opportunity_id: str
    contact_id: str
    account_id: str | None
    name: str
    amount: Decimal | None
    currency: str
    stage: str
    expected_close_date: date | None
    closed_at: datetime | None
    close_reason: str | None
    owner_agent_id: str | None
    created_at: datetime
    updated_at: datetime


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Opportunities are always tenant-scoped; a global caller has no
    tenant_id and therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Opportunity repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def create_opportunity(
    db: Database,
    claims: AuthClaims,
    *,
    contact_id: str,
    account_id: str | None,
    name: str,
    amount: Decimal | None,
    currency: str,
    expected_close_date: date | None = None,
    owner_agent_id: str | None = None,
) -> str:
    """Insert a new ``opportunities`` row. Returns the ``opportunity_id``
    (uuid4().hex). Always starts at ``stage='prospecting'`` (D1).

    Does NOT validate that ``contact_id``/``account_id`` belong to this
    tenant -- callers (routes) are expected to have already resolved/
    validated them (``INVALID_CONTACT``/``INVALID_ACCOUNT`` at the route
    layer). Does NOT validate ``amount >= 0`` -- callers validate before
    calling (defense-in-depth also lives at the DB CHECK constraint).
    """
    _reject_global(claims)

    new_opportunity_id = uuid4().hex
    await db.execute(
        "INSERT INTO opportunities "
        "(tenant_id, opportunity_id, contact_id, account_id, name, amount, "
        " currency, stage, expected_close_date, owner_agent_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, 'prospecting', $8, $9)",
        claims.tenant_id,
        new_opportunity_id,
        contact_id,
        account_id,
        name,
        amount,
        currency,
        expected_close_date,
        owner_agent_id,
    )
    return new_opportunity_id


async def get_opportunity(
    db: Database,
    claims: AuthClaims,
    opportunity_id: str,
) -> Opportunity | None:
    """Fetch an opportunity by ``opportunity_id`` scoped to the caller's
    tenant, or ``None``."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT opportunity_id, contact_id, account_id, name, amount, currency, "
        "stage, expected_close_date, closed_at, close_reason, owner_agent_id, "
        "created_at, updated_at "
        "FROM opportunities "
        "WHERE tenant_id = $1 AND opportunity_id = $2",
        claims.tenant_id,
        opportunity_id,
    )
    return _row_to_opportunity(row) if row is not None else None


async def list_opportunities(
    db: Database,
    claims: AuthClaims,
    *,
    limit: int = 50,
    offset: int = 0,
    stage: str | None = None,
    contact_id: str | None = None,
    account_id: str | None = None,
    owner_agent_id: str | None = None,
    open_only: bool = False,
) -> tuple[list[Opportunity], int]:
    """Fetch a paginated, filtered page of the caller's tenant opportunities,
    newest first.

    Tenant-scoped (``WHERE tenant_id = $1``); each supplied filter appends
    exactly one positional ``AND`` clause, values always bound (never
    interpolated). ``open_only=True`` appends
    ``AND stage NOT IN ('closed_won','closed_lost')``. Returns
    ``(rows, total)`` -- ``total`` is a ``count(*)`` over the same filtered
    WHERE (minus LIMIT/OFFSET).
    """
    _reject_global(claims)

    where = "WHERE tenant_id = $1"
    params: list[Any] = [claims.tenant_id]

    if stage is not None:
        params.append(stage)
        where += f" AND stage = ${len(params)}"
    if contact_id is not None:
        params.append(contact_id)
        where += f" AND contact_id = ${len(params)}"
    if account_id is not None:
        params.append(account_id)
        where += f" AND account_id = ${len(params)}"
    if owner_agent_id is not None:
        params.append(owner_agent_id)
        where += f" AND owner_agent_id = ${len(params)}"
    if open_only:
        where += " AND stage NOT IN ('closed_won','closed_lost')"

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    count_row = await db.fetchrow(
        "SELECT count(*) AS count FROM opportunities " + where, *params,
    )
    total = int(count_row["count"]) if count_row is not None else 0

    clamped_limit = max(1, min(limit, 200))
    page_params = [*params, clamped_limit, max(0, offset)]
    limit_idx = len(page_params) - 1
    offset_idx = len(page_params)
    rows = await db.fetch(
        "SELECT opportunity_id, contact_id, account_id, name, amount, currency, "
        "stage, expected_close_date, closed_at, close_reason, owner_agent_id, "
        "created_at, updated_at "
        "FROM opportunities " + where +
        f" ORDER BY created_at DESC, opportunity_id DESC LIMIT ${limit_idx} OFFSET ${offset_idx}",
        *page_params,
    )
    return [_row_to_opportunity(row) for row in rows], total


_UNSET: Any = object()
"""Sentinel meaning "field not supplied" for ``update_opportunity``'s partial
update. Distinct from ``None``, which is a valid, explicit "clear this
field" value (e.g. ``expected_close_date=None`` clears the forecast date)."""


async def update_opportunity(
    db: Database,
    claims: AuthClaims,
    opportunity_id: str,
    *,
    name: str | None = _UNSET,
    amount: Decimal | None = _UNSET,
    expected_close_date: date | None = _UNSET,
    owner_agent_id: str | None = _UNSET,
    account_id: str | None = _UNSET,
) -> Opportunity | None:
    """Partially update an opportunity, tenant-scoped. Only supplied fields
    change. Deliberately does NOT accept ``stage`` or ``currency`` -- those
    are immutable via PATCH (stage changes go through ``transition_stage``;
    currency is a historical snapshot, D7).

    Uses the ``_UNSET`` sentinel as the "not supplied" default so ``None``
    can be passed explicitly to clear a field (e.g.
    ``expected_close_date=None``). Returns the updated ``Opportunity``, or
    ``None`` if no row matched (missing ``opportunity_id`` or cross-tenant
    access).
    """
    _reject_global(claims)

    set_clauses: list[str] = []
    params: list[Any] = []

    for column, value in (
        ("name", name),
        ("amount", amount),
        ("expected_close_date", expected_close_date),
        ("owner_agent_id", owner_agent_id),
        ("account_id", account_id),
    ):
        if value is not _UNSET:
            params.append(value)
            set_clauses.append(f"{column} = ${len(params)}")

    if not set_clauses:
        # Nothing to update; just return the current row (tenant-scoped read).
        return await get_opportunity(db, claims, opportunity_id)

    params.append(claims.tenant_id)
    tenant_idx = len(params)
    params.append(opportunity_id)
    opportunity_idx = len(params)

    # Parameterized SQL; set_clauses built from a fixed, safe column allowlist.
    # ruff: noqa: S608
    result = await db.execute(
        "UPDATE opportunities SET " + ", ".join(set_clauses) + ", updated_at = now() "
        f"WHERE tenant_id = ${tenant_idx} AND opportunity_id = ${opportunity_idx}",
        *params,
    )
    if _rows_affected(result) == 0:
        return None

    return await get_opportunity(db, claims, opportunity_id)


async def transition_stage(
    db: Database,
    claims: AuthClaims,
    opportunity_id: str,
    *,
    stage: str,
    close_reason: str | None,
    closed_at: datetime | None,
) -> Opportunity | None:
    """Set ``stage`` (and ``close_reason``/``closed_at`` when terminal) on an
    opportunity, tenant-scoped. Returns the updated ``Opportunity``, or
    ``None`` if no row matched (missing ``opportunity_id`` or cross-tenant
    access).

    Does NOT itself validate the transition -- callers (the admin route)
    call ``opportunities.pipeline.validate_transition`` first (D1) and
    enforce ``close_reason`` presence on ``closed_lost`` (D9) before
    reaching this method, so a bad transition never reaches the DB.
    """
    _reject_global(claims)

    result = await db.execute(
        "UPDATE opportunities SET stage = $1, close_reason = $2, closed_at = $3, "
        "updated_at = now() "
        "WHERE tenant_id = $4 AND opportunity_id = $5",
        stage,
        close_reason,
        closed_at,
        claims.tenant_id,
        opportunity_id,
    )
    if _rows_affected(result) == 0:
        return None

    return await get_opportunity(db, claims, opportunity_id)


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


def _row_to_opportunity(row: Any) -> Opportunity:
    return Opportunity(
        opportunity_id=str(row["opportunity_id"]),
        contact_id=str(row["contact_id"]),
        account_id=row["account_id"],
        name=str(row["name"]),
        amount=row["amount"],
        currency=str(row["currency"]),
        stage=str(row["stage"]),
        expected_close_date=row["expected_close_date"],
        closed_at=row["closed_at"],
        close_reason=row["close_reason"],
        owner_agent_id=row["owner_agent_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
