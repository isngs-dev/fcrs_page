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
) -> tuple[list[Account], int]:
    """Fetch a paginated page of the caller's tenant accounts, newest first.

    Returns ``(rows, total)`` -- ``total`` is a ``count(*)`` over the
    tenant-scoped WHERE (minus LIMIT/OFFSET).
    """
    _reject_global(claims)

    count_row = await db.fetchrow(
        "SELECT count(*) AS count FROM accounts WHERE tenant_id = $1",
        claims.tenant_id,
    )
    total = int(count_row["count"]) if count_row is not None else 0

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)
    rows = await db.fetch(
        "SELECT account_id, name, domain, created_at, updated_at "
        "FROM accounts "
        "WHERE tenant_id = $1 "
        "ORDER BY created_at DESC, account_id DESC "
        "LIMIT $2 OFFSET $3",
        claims.tenant_id,
        clamped_limit,
        clamped_offset,
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
