"""Auth repository -- pre-auth identity resolution.

``get_user_by_email`` is intentionally UNSCOPED (no ``tenant_id`` filter).
At login time there are no AuthClaims yet; the query resolves identity by
email so the caller can verify the password and build claims from the
resulting row. This is the one legitimate pre-auth database query.
"""
from __future__ import annotations

from typing import Any

from common.db import Database

Row = dict[str, Any]


async def get_user_by_email(db: Database, email: str) -> Row | None:
    """Look up a user by email (case-insensitive). Returns the full row or None.

    UNSCOPED by design -- this is the pre-auth identity-resolution query.
    The caller uses the row's ``tenant_id``/``client_account_id`` and
    ``role`` to build AuthClaims; neither is ever accepted from user input.
    """
    sql = (
        "SELECT id, tenant_id, client_account_id, email, role, password_hash, "
        "name, active, last_login_at "
        "FROM users WHERE lower(email) = lower($1)"
    )
    record = await db.fetchrow(sql, email)
    return dict(record) if record is not None else None


async def get_user_by_id(db: Database, user_id: str) -> Row | None:
    """Look up a user by id. Returns the full row or None.

    Parameterized (``WHERE id = $1``). Callers that need tenant-scoped
    validation (e.g. lead assignment) must check the returned row's
    ``tenant_id``/``role``/``active`` themselves -- this is a plain identity
    lookup, not a tenant-scoped query, since ``users`` has no repository of
    its own yet.
    """
    sql = (
        "SELECT id, tenant_id, client_account_id, email, role, password_hash, "
        "name, active, last_login_at "
        "FROM users WHERE id = $1"
    )
    record = await db.fetchrow(sql, user_id)
    return dict(record) if record is not None else None


async def get_default_tenant_id_for_account(db: Database, account_id: str) -> str | None:
    """The account's earliest-created ENABLED tenant, or ``None`` if it has
    none -- the deterministic "active tenant" a login resolves to. Computed
    on demand (no stored "default tenant" column that could go stale)."""
    sql = (
        "SELECT id FROM tenants WHERE client_account_id = $1 AND enabled "
        "ORDER BY created_at ASC LIMIT 1"
    )
    row = await db.fetchrow(sql, account_id)
    return str(row["id"]) if row is not None else None


async def get_tenant_for_switch(db: Database, tenant_id: str) -> Row | None:
    """Minimal tenant lookup for ``POST /auth/switch-tenant`` -- just enough
    to validate the target belongs to the caller's own account and is
    enabled. Unscoped by design (the caller re-checks ``client_account_id``
    against their own account themselves; there is no ``AuthClaims`` yet
    scoped to the *target* tenant at this point)."""
    sql = (
        "SELECT id, name, slug, enabled, client_account_id "
        "FROM tenants WHERE id = $1"
    )
    record = await db.fetchrow(sql, tenant_id)
    return dict(record) if record is not None else None


async def set_password_hash(db: Database, user_id: str, new_hash: str) -> None:
    """Update a user's password hash. Parameterized; no tenant filter needed
    (the caller already validated the reset token for this user_id).
    """
    await db.execute(
        "UPDATE users SET password_hash = $1 WHERE id = $2",
        new_hash,
        user_id,
    )
