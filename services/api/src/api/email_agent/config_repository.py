"""Per-tenant Email Agent sender config (A2 E1). No platform default.

The SendGrid API key is AES-256-GCM encrypted with ``SecretBox`` (AAD = tenant
id, so a ciphertext copied to another tenant's row won't decrypt) and only
decrypted when building a provider. Never returned by the API.
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.crypto import SecretBox
from common.db import Database

from api.agents.repository import _reject_global
from api.config import get_api_settings


@dataclass(frozen=True)
class EmailAgentConfig:
    provider: str  # sendgrid | smtp
    from_address: str
    from_name: str | None
    reply_to: str | None
    booking_url: str | None
    has_api_key: bool
    enabled: bool
    reply_domain: str | None = None
    event_webhook_public_key: str | None = None  # public key, not a secret


def _box() -> SecretBox:
    return SecretBox(get_api_settings().secret_encryption_key)


async def get_email_agent_config(db: Database, claims: AuthClaims) -> EmailAgentConfig | None:
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT provider, from_address, from_name, reply_to, booking_url, "
        "api_key_ciphertext IS NOT NULL AS has_api_key, enabled, reply_domain, "
        "event_webhook_public_key "
        "FROM tenant_email_agent_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    return EmailAgentConfig(**dict(row)) if row is not None else None


async def get_sendgrid_api_key(db: Database, claims: AuthClaims) -> str | None:
    """The decrypted SendGrid key, or ``None`` when none is stored."""
    _reject_global(claims)
    ciphertext = await db.fetchval(
        "SELECT api_key_ciphertext FROM tenant_email_agent_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if ciphertext is None:
        return None
    return _box().decrypt_str(str(ciphertext), aad=str(claims.tenant_id).encode())


async def upsert_email_agent_config(
    db: Database,
    claims: AuthClaims,
    *,
    provider: str,
    from_address: str,
    from_name: str | None,
    reply_to: str | None,
    booking_url: str | None,
    enabled: bool,
    api_key: str | None,
    reply_domain: str | None = None,
    event_webhook_public_key: str | None = None,
) -> None:
    """Upsert the config. ``api_key=None`` keeps the stored key; ``""`` clears it."""
    _reject_global(claims)
    ciphertext = (_box().encrypt(api_key, aad=str(claims.tenant_id).encode())
                  if api_key else None)
    await db.execute(
        "INSERT INTO tenant_email_agent_configs (tenant_id, provider, from_address, from_name, "
        "reply_to, booking_url, api_key_ciphertext, enabled, updated_by, reply_domain, "
        "event_webhook_public_key) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $11, $12) "
        "ON CONFLICT (tenant_id) DO UPDATE SET provider = EXCLUDED.provider, "
        "from_address = EXCLUDED.from_address, from_name = EXCLUDED.from_name, "
        "reply_to = EXCLUDED.reply_to, booking_url = EXCLUDED.booking_url, "
        "api_key_ciphertext = CASE WHEN $10 THEN EXCLUDED.api_key_ciphertext "
        "ELSE tenant_email_agent_configs.api_key_ciphertext END, "
        "enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by, updated_at = now(), "
        "reply_domain = EXCLUDED.reply_domain, "
        "event_webhook_public_key = EXCLUDED.event_webhook_public_key",
        claims.tenant_id, provider, from_address, from_name, reply_to, booking_url, ciphertext,
        enabled, claims.subject, api_key is not None, reply_domain, event_webhook_public_key,
    )
