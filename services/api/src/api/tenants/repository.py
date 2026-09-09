"""Tenant repository — hand-built on common.db + common.tenancy.

The ``tenants`` table has no ``tenant_id`` column; it IS the tenant, so
``tenant_filter`` is called with ``column="id"`` to scope reads by the
table's own primary key.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from common.auth import AuthClaims, Role
from common.db import Database
from common.tenancy import require_role, tenant_filter

Row = dict[str, Any]


class TenantRepository:
    """Async tenant CRUD scoped via AuthClaims."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, claims: AuthClaims, id: str) -> Row | None:
        """Fetch a single tenant by id, respecting tenant scope."""
        frag, fparams = tenant_filter(claims, next_param=2, column="id")
        sql = f"SELECT * FROM tenants WHERE id = $1 {frag}"  # noqa: S608
        row = await self.db.fetchrow(sql, id, *fparams)
        return dict(row) if row is not None else None

    async def list(self, claims: AuthClaims) -> list[Row]:
        """List tenants visible to the caller."""
        frag, fparams = tenant_filter(claims, next_param=1, column="id")
        sql = f"SELECT * FROM tenants WHERE TRUE {frag}"  # noqa: S608
        rows = await self.db.fetch(sql, *fparams)
        return [dict(r) for r in rows]

    async def create(self, claims: AuthClaims, data: Row) -> Row:
        """Create a new tenant. Requires PLATFORM_ADMIN."""
        require_role(claims, Role.PLATFORM_ADMIN)
        tenant_id = uuid4().hex
        name: str = data["name"]
        slug: str = data["slug"]
        enabled: bool = data.get("enabled", True)
        sql = (
            "INSERT INTO tenants (id, name, slug, enabled) "
            "VALUES ($1, $2, $3, $4) RETURNING *"
        )
        row = await self.db.fetchrow(sql, tenant_id, name, slug, enabled)
        assert row is not None  # noqa: S101
        return dict(row)


class ClientAccountRepository:
    """Read-only ``client_accounts`` access -- PLATFORM_ADMIN-only, global by
    construction (multi-chatbot accounts). Backs `/debug/client-accounts`,
    which the platform-admin `/clients` list uses purely to GROUP its
    existing flat tenant list by account -- no other consumer exists yet.

    Deliberately NOT folded into `TenantRepository`'s `tenant_filter`
    convention: joining `client_accounts` there would make `tenant_filter`'s
    bare `column="id"` filter ambiguous (both tables have an `id` column),
    and that helper's column-identifier validation exists specifically to
    keep injection-proofing simple -- not worth reworking for one debug
    route. `client_account_id` already flows through `TenantRepository`'s
    own `SELECT *` automatically once the column exists (migration 0060).
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def list(self, claims: AuthClaims) -> list[Row]:
        """List every client account. Requires PLATFORM_ADMIN (global)."""
        require_role(claims, Role.PLATFORM_ADMIN)
        rows = await self.db.fetch("SELECT * FROM client_accounts ORDER BY created_at")
        return [dict(r) for r in rows]
