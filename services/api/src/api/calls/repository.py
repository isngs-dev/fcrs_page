"""Per-tenant call config repository -- no secrets, nothing encrypted.

Mirrors ``api.scheduling.calendar_config_repository`` structurally (a small,
dedicated one-row-per-tenant config table with a claims-scoped pair for the
admin surface plus one claims-less read for a webhook), but unlike calendar/
notification config there is no credential column here to encrypt: a phone
number and an admin-authored SMS template are not secrets.
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


@dataclass(frozen=True)
class CallConfig:
    """A tenant's missed-call text-back config."""

    monitored_phone_number: str
    enabled: bool
    text_back_message: str


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Call config is always tenant-scoped; a global caller has no tenant_id
    and therefore cannot be filtered to a tenant's row.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Call config repository is tenant-scoped; PLATFORM_ADMIN callers "
            "are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_call_config(db: Database, claims: AuthClaims) -> CallConfig | None:
    """Fetch the caller's tenant call config.

    Returns ``None`` if missed-call text-back has never been configured for
    the tenant. Raises ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT monitored_phone_number, enabled, text_back_message "
        "FROM tenant_call_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if row is None:
        return None
    return _row_to_call_config(row)


async def get_call_config_by_tenant_id(db: Database, tenant_id: str) -> CallConfig | None:
    """Fetch a tenant's call config by RAW tenant id -- NO ``AuthClaims``.

    Used ONLY by the Twilio call-status webhook receiver
    (``api.calls.webhook``), which has no session/claims at all: the
    tenant's own already-configured Twilio Auth Token (read separately, via
    ``api.notifications.config_repository``) is what verifies the webhook's
    signature -- the path ``{tenant_id}`` is never trusted as authentication
    on its own, only used to look up which tenant's secret to verify
    against. Mirrors ``get_calendar_config_by_tenant_id``'s own documented
    justification for being the one legitimate claims-less caller.
    """
    row = await db.fetchrow(
        "SELECT monitored_phone_number, enabled, text_back_message "
        "FROM tenant_call_configs WHERE tenant_id = $1",
        tenant_id,
    )
    if row is None:
        return None
    return _row_to_call_config(row)


def _row_to_call_config(row: object) -> CallConfig:
    return CallConfig(
        monitored_phone_number=str(row["monitored_phone_number"]),  # type: ignore[index]
        enabled=bool(row["enabled"]),  # type: ignore[index]
        text_back_message=str(row["text_back_message"]),  # type: ignore[index]
    )


async def upsert_call_config(
    db: Database,
    claims: AuthClaims,
    *,
    monitored_phone_number: str,
    enabled: bool,
    text_back_message: str,
) -> None:
    """Insert or update the caller's tenant call config.

    Raises ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    await db.execute(
        "INSERT INTO tenant_call_configs "
        "(tenant_id, monitored_phone_number, enabled, text_back_message) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "monitored_phone_number = $2, enabled = $3, text_back_message = $4, updated_at = now()",
        claims.tenant_id,
        monitored_phone_number,
        enabled,
        text_back_message,
    )
