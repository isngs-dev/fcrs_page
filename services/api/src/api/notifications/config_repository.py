"""Per-tenant, per-channel notification config repository (S9.1, re-keyed S9.3).

Mirrors ``api.scheduling.calendar_config_repository``: ``credentials_ciphertext``
stores the channel's secret credential AES-256-GCM encrypted (``SecretBox``)
-- the SMTP password for ``channel="email"``, the Twilio Auth Token for
``channel="sms"``/``"whatsapp"`` -- decrypted only when building a
``NotificationProvider`` (``api.notifications.providers.notification_provider_for``).
Never logged or echoed by the route layer.

S9.3 re-keys ``tenant_notification_configs`` to ``PRIMARY KEY (tenant_id,
channel)`` (migration 0026) so a tenant may hold an independent config row
per channel -- ``email`` and ``sms`` and ``whatsapp`` simultaneously. Every
read/write is now channel-scoped: ``get_notification_config`` takes a
required ``channel`` argument; ``upsert_notification_config`` upserts
``ON CONFLICT (tenant_id, channel)``. The S9.1/S9.2 email flow is unaffected
-- existing rows were backfilled to ``channel='email'`` by the migration.
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.crypto import SecretBox
from common.db import Database
from common.errors import ValidationError

from api.config import get_api_settings


@dataclass(frozen=True)
class NotificationConfig:
    """A tenant's per-channel notification config, secret credential decrypted."""

    provider: str
    channel: str
    from_address: str | None
    from_name: str | None
    smtp_host: str | None
    smtp_port: int | None
    smtp_use_tls: bool
    smtp_username: str | None
    twilio_account_sid: str | None
    twilio_from: str | None
    credentials: str  # DECRYPTED (SMTP password OR Twilio Auth Token); "" when unset
    enabled: bool


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Notification config is always tenant-scoped; a global caller has no
    tenant_id and therefore cannot be filtered to a tenant's row.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Notification config repository is tenant-scoped; PLATFORM_ADMIN "
            "callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_notification_config(
    db: Database, claims: AuthClaims, channel: str
) -> NotificationConfig | None:
    """Fetch the caller's tenant notification config for ``channel``, credential decrypted.

    Returns ``None`` if no notification config has been set for this
    tenant+channel. Raises ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT provider, from_address, from_name, smtp_host, smtp_port, smtp_use_tls, "
        "smtp_username, twilio_account_sid, twilio_from, credentials_ciphertext, enabled "
        "FROM tenant_notification_configs WHERE tenant_id = $1 AND channel = $2",
        claims.tenant_id,
        channel,
    )
    if row is None:
        return None

    credentials = ""
    ciphertext = row["credentials_ciphertext"]
    if ciphertext:
        box = SecretBox(get_api_settings().secret_encryption_key)
        credentials = box.decrypt_str(str(ciphertext))

    return NotificationConfig(
        provider=str(row["provider"]),
        channel=channel,
        from_address=str(row["from_address"]) if row["from_address"] is not None else None,
        from_name=str(row["from_name"]) if row["from_name"] is not None else None,
        smtp_host=str(row["smtp_host"]) if row["smtp_host"] is not None else None,
        smtp_port=int(row["smtp_port"]) if row["smtp_port"] is not None else None,
        smtp_use_tls=bool(row["smtp_use_tls"]),
        smtp_username=str(row["smtp_username"]) if row["smtp_username"] is not None else None,
        twilio_account_sid=(
            str(row["twilio_account_sid"]) if row["twilio_account_sid"] is not None else None
        ),
        twilio_from=str(row["twilio_from"]) if row["twilio_from"] is not None else None,
        credentials=credentials,
        enabled=bool(row["enabled"]),
    )


async def get_notification_config_by_tenant_id(
    db: Database, tenant_id: str, channel: str
) -> NotificationConfig | None:
    """Fetch a tenant's per-channel notification config by RAW tenant id --
    NO ``AuthClaims``.

    Used ONLY by ``api.calls.webhook`` (the Twilio call-status webhook),
    which has no session/claims at all: it needs the tenant's already-stored
    Twilio Auth Token (``channel="sms"``) purely to VERIFY the inbound
    webhook's signature, before trusting anything else in the request.
    Mirrors ``api.scheduling.calendar_config_repository
    .get_calendar_config_by_tenant_id``'s own documented justification for
    being the one legitimate claims-less caller. Every other caller in this
    module MUST go through the ``AuthClaims``-scoped ``get_notification_config``
    above.
    """
    row = await db.fetchrow(
        "SELECT provider, from_address, from_name, smtp_host, smtp_port, smtp_use_tls, "
        "smtp_username, twilio_account_sid, twilio_from, credentials_ciphertext, enabled "
        "FROM tenant_notification_configs WHERE tenant_id = $1 AND channel = $2",
        tenant_id,
        channel,
    )
    if row is None:
        return None

    credentials = ""
    ciphertext = row["credentials_ciphertext"]
    if ciphertext:
        box = SecretBox(get_api_settings().secret_encryption_key)
        credentials = box.decrypt_str(str(ciphertext))

    return NotificationConfig(
        provider=str(row["provider"]),
        channel=channel,
        from_address=str(row["from_address"]) if row["from_address"] is not None else None,
        from_name=str(row["from_name"]) if row["from_name"] is not None else None,
        smtp_host=str(row["smtp_host"]) if row["smtp_host"] is not None else None,
        smtp_port=int(row["smtp_port"]) if row["smtp_port"] is not None else None,
        smtp_use_tls=bool(row["smtp_use_tls"]),
        smtp_username=str(row["smtp_username"]) if row["smtp_username"] is not None else None,
        twilio_account_sid=(
            str(row["twilio_account_sid"]) if row["twilio_account_sid"] is not None else None
        ),
        twilio_from=str(row["twilio_from"]) if row["twilio_from"] is not None else None,
        credentials=credentials,
        enabled=bool(row["enabled"]),
    )


async def upsert_notification_config(
    db: Database,
    claims: AuthClaims,
    *,
    channel: str,
    provider: str,
    from_address: str | None,
    from_name: str | None,
    smtp_host: str | None,
    smtp_port: int | None,
    smtp_use_tls: bool,
    smtp_username: str | None,
    twilio_account_sid: str | None,
    twilio_from: str | None,
    credentials: str,
    enabled: bool,
) -> None:
    """Insert or update the caller's tenant+channel notification config.

    The secret credential (SMTP password OR Twilio Auth Token) is encrypted
    at rest. Upserts ``ON CONFLICT (tenant_id, channel)`` -- a tenant may
    hold an independent row per channel (``email``/``sms``/``whatsapp``).
    Raises ``ValidationError`` for global callers.
    """
    _reject_global(claims)

    box = SecretBox(get_api_settings().secret_encryption_key)
    ciphertext = box.encrypt(credentials) if credentials else None

    await db.execute(
        "INSERT INTO tenant_notification_configs "
        "(tenant_id, channel, provider, from_address, from_name, smtp_host, smtp_port, "
        "smtp_use_tls, smtp_username, twilio_account_sid, twilio_from, "
        "credentials_ciphertext, enabled) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
        "ON CONFLICT (tenant_id, channel) DO UPDATE SET "
        "provider = $3, from_address = $4, from_name = $5, smtp_host = $6, "
        "smtp_port = $7, smtp_use_tls = $8, smtp_username = $9, "
        "twilio_account_sid = $10, twilio_from = $11, "
        "credentials_ciphertext = $12, enabled = $13, updated_at = now()",
        claims.tenant_id,
        channel,
        provider,
        from_address,
        from_name,
        smtp_host,
        smtp_port,
        smtp_use_tls,
        smtp_username,
        twilio_account_sid,
        twilio_from,
        ciphertext,
        enabled,
    )
