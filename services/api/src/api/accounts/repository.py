"""Accounts repository — tenant-scoped async SQL for the Account entity.

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …).
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

Data model (migration 0040):
- ``accounts(tenant_id PK, account_id PK, name, domain, created_at,
  updated_at)``. An Account is always optional (SR-9.2 D3) -- never
  auto-created by this module or any caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


@dataclass(frozen=True)
class Account:
    """A single account (company) row."""

    account_id: str
    name: str
    domain: str | None
    created_at: datetime
    updated_at: datetime


# SR-29: ORDER BY identifiers cannot be bound parameters in Postgres. The
# VALUES in this dict are the only SQL fragments ever accepted for list
# ordering; the caller's string is used exclusively as a dictionary KEY and
# is never interpolated into SQL. This dict-membership check is the ONLY
# thing preventing SQL injection on this path.
_SORT_COLUMNS: dict[str, str] = {
    "name": "name",
    "domain": "domain",
    "created": "created_at",
}
ACCOUNT_SORT_KEYS = frozenset(_SORT_COLUMNS)

_DEFAULT_SORT = "created"
_DEFAULT_DIRECTION = "desc"
ACCOUNT_SORT_DEFAULT_DIRECTIONS: dict[str, str] = {
    "name": "asc",
    "domain": "asc",
    "created": "desc",
}


def _escape_ilike_pattern(value: str) -> str:
    """Escape user wildcard characters before binding a literal substring."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Accounts are always tenant-scoped; a global caller has no tenant_id and
    therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Account repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def create_account(
    db: Database,
    claims: AuthClaims,
    *,
    name: str,
    domain: str | None = None,
) -> str:
    """Insert a new ``accounts`` row. Returns the ``account_id`` (uuid4().hex)."""
    _reject_global(claims)

    new_account_id = uuid4().hex
    await db.execute(
        "INSERT INTO accounts (tenant_id, account_id, name, domain) "
        "VALUES ($1, $2, $3, $4)",
        claims.tenant_id,
        new_account_id,
        name,
        domain,
    )
    return new_account_id


async def get_account(
    db: Database,
    claims: AuthClaims,
    account_id: str,
) -> Account | None:
    """Fetch an account by ``account_id`` scoped to the caller's tenant, or ``None``."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT account_id, name, domain, created_at, updated_at "
        "FROM accounts "
        "WHERE tenant_id = $1 AND account_id = $2",
        claims.tenant_id,
        account_id,
    )
    return _row_to_account(row) if row is not None else None


async def list_accounts(
    db: Database,
    claims: AuthClaims,
    *,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    sort: str = _DEFAULT_SORT,
    direction: str = _DEFAULT_DIRECTION,
) -> tuple[list[Account], int]:
    """Fetch a paginated, sorted, optionally-searched page of the caller's
    tenant accounts.

    ``sort``/``direction`` are validated here (defense-in-depth; the route
    layer validates first) because ORDER BY structure cannot be bound as a
    parameter. ``q`` is a literal, case-insensitive Name-or-Domain substring
    match -- wildcard characters are escaped before binding so a search for
    a literal ``%`` or ``_`` does not silently become a match-all/match-any
    pattern.

    Returns ``(rows, total)`` -- ``total`` is a ``count(*)`` over the same
    filtered WHERE (minus LIMIT/OFFSET), so it stays consistent with
    ``items`` under search. Ordered by the SR-29 closed allowlist with
    ``account_id DESC`` as the mandatory total-order tie-break (required for
    pagination correctness, not cosmetic).
    """
    _reject_global(claims)

    if sort not in _SORT_COLUMNS:
        raise ValidationError(f"Unknown sort key {sort!r}.", code="INVALID_ACCOUNT_SORT")
    if direction not in {"asc", "desc"}:
        raise ValidationError(
            f"Unknown sort direction {direction!r}.", code="INVALID_ACCOUNT_SORT_DIRECTION",
        )

    where = "WHERE tenant_id = $1"
    params: list[Any] = [claims.tenant_id]

    if q:
        escaped_q = _escape_ilike_pattern(q)
        params.extend([f"%{escaped_q}%", f"%{escaped_q}%"])
        name_idx = len(params) - 1
        domain_idx = len(params)
        where += (
            f" AND (name ILIKE ${name_idx} ESCAPE '\\' "
            f"OR domain ILIKE ${domain_idx} ESCAPE '\\')"
        )

    sort_expr = _SORT_COLUMNS[sort]  # constant; never caller input
    direction_sql = "ASC" if direction == "asc" else "DESC"
    # D-NULLS: NULLS LAST in BOTH directions -- an absent value is not a low value.
    # D-TIEBREAK: `account_id DESC` is MANDATORY; without a total order, pagination
    # can silently duplicate or skip rows across pages.
    order_by = f"ORDER BY {sort_expr} {direction_sql} NULLS LAST, account_id DESC"

    # Parameterized SQL; `where` is built above from bound params only and
    # `order_by` is constrained to the developer-authored allowlist above.
    # ruff: noqa: S608
    count_row = await db.fetchrow(
        "SELECT count(*) AS count FROM accounts " + where, *params,
    )
    total = int(count_row["count"]) if count_row is not None else 0

    clamped_limit = max(1, min(limit, 200))
    page_params = [*params, clamped_limit, max(0, offset)]
    limit_idx = len(page_params) - 1
    offset_idx = len(page_params)
    rows = await db.fetch(
        "SELECT account_id, name, domain, created_at, updated_at "
        "FROM accounts " + where + " "
        f"{order_by} LIMIT ${limit_idx} OFFSET ${offset_idx}",
        *page_params,
    )
    return [_row_to_account(row) for row in rows], total


def _row_to_account(row: Any) -> Account:
    return Account(
        account_id=str(row["account_id"]),
        name=str(row["name"]),
        domain=row["domain"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
