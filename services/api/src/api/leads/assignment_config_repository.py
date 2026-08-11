"""Round-robin assignment config repository (SR-20 D1/D4) -- the per-tenant
``round_robin_enabled`` flag and its rotation cursor.

Mirrors ``api.orchestrator.config_repository``'s never-``None`` resolver
shape exactly: an unconfigured tenant resolves to
``settings.assignment_round_robin_default`` (D1: default OFF) rather than
``None``, so a caller never has to special-case "no config yet".

Deliberately kept SEPARATE from the rotation-cursor advance in
``api.leads.assignment`` (D4): ``upsert_assignment_config`` here only ever
writes ``round_robin_enabled`` (the human-facing toggle) and NEVER touches
``last_assigned_agent_id`` (the rotation cursor) -- that column has exactly
one writer, the atomic ``UPDATE ... RETURNING`` in ``assignment.py``, so a
settings-page save can never race with or clobber an in-flight rotation.

Data model (migration TBD -- see migrations/versions, read live head first
per D8):
- ``tenant_assignment_configs(tenant_id PK, round_robin_enabled boolean,
  last_assigned_agent_id text, created_at, updated_at)``.
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.config import get_api_settings


@dataclass(frozen=True)
class AssignmentConfig:
    """A tenant's round-robin assignment config (D1) + rotation cursor (D4).

    Never ``None`` -- see ``get_assignment_config``. ``round_robin_enabled``
    defaults to ``False`` for an unconfigured tenant, so an un-opted tenant's
    ``create_lead`` behavior is byte-identical to pre-SR-20 (M2).
    """

    round_robin_enabled: bool
    last_assigned_agent_id: str | None = None


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Assignment config is always tenant-scoped; a global caller has no
    tenant_id and therefore cannot be filtered to a tenant's row.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Assignment config repository is tenant-scoped; PLATFORM_ADMIN "
            "callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_assignment_config(db: Database, claims: AuthClaims) -> AssignmentConfig:
    """Fetch the caller's tenant assignment config, or the settings default.

    Never returns ``None`` -- an unconfigured tenant is deterministic via
    ``settings.assignment_round_robin_default`` (D1: OFF). Raises
    ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT round_robin_enabled, last_assigned_agent_id "
        "FROM tenant_assignment_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if row is None:
        settings = get_api_settings()
        return AssignmentConfig(
            round_robin_enabled=settings.assignment_round_robin_default,
            last_assigned_agent_id=None,
        )

    return AssignmentConfig(
        round_robin_enabled=bool(row["round_robin_enabled"]),
        last_assigned_agent_id=row["last_assigned_agent_id"],
    )


async def upsert_assignment_config(
    db: Database,
    claims: AuthClaims,
    *,
    round_robin_enabled: bool,
) -> None:
    """Insert or update ONLY the ``round_robin_enabled`` toggle for the
    caller's tenant.

    Never binds/updates ``last_assigned_agent_id`` -- the rotation cursor is
    owned exclusively by ``api.leads.assignment``'s atomic advance step
    (D4), so a settings-page save can never race with or reset an
    in-flight rotation. On first insert for a tenant, the cursor starts
    ``NULL`` (start of pool) via the table's own DEFAULT/nullability, not a
    literal bound here.
    """
    _reject_global(claims)

    await db.execute(
        "INSERT INTO tenant_assignment_configs (tenant_id, round_robin_enabled) "
        "VALUES ($1, $2) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "round_robin_enabled = $2, updated_at = now()",
        claims.tenant_id,
        round_robin_enabled,
    )
