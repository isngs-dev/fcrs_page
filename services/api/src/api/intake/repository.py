"""Lead intake config, Meta page routes and intake events (migration 0074).

Tenant methods take ``AuthClaims`` and filter by ``claims.tenant_id``; the two
``*_system`` lookups resolve an inbound Meta webhook to its tenant (a page belongs
to exactly one tenant) or read a page token for the outbox handler.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from asyncpg.exceptions import UniqueViolationError
from common.auth import AuthClaims
from common.crypto import SecretBox
from common.db import Database
from common.errors import ConflictError

from api.agents.repository import _reject_global
from api.config import get_api_settings

DEFAULT_TRUSTED = ("facebookmail.com",)


@dataclass(frozen=True)
class IntakeConfig:
    enabled: bool = True
    trusted_senders: tuple[str, ...] = DEFAULT_TRUSTED
    form_consent_wording: str | None = None
    call_consent_wording: str | None = None
    meta_page_id: str | None = None
    has_meta_page_token: bool = False


@dataclass(frozen=True)
class IntakeEvent:
    intake_id: str
    source: str
    external_id: str
    status: str
    reason: str | None
    lead_id: str | None
    fields: dict[str, Any]
    received_at: datetime
    resolved_by: str | None


def _box() -> SecretBox:
    return SecretBox(get_api_settings().secret_encryption_key)


async def get_config(db: Database, claims: AuthClaims) -> IntakeConfig:
    """The tenant's config, or the defaults when none is saved yet."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT enabled, trusted_senders, form_consent_wording, call_consent_wording, "
        "meta_page_id, meta_page_token_ciphertext IS NOT NULL AS has_meta_page_token "
        "FROM tenant_lead_intake_configs WHERE tenant_id = $1", claims.tenant_id)
    if row is None:
        return IntakeConfig()
    values = dict(row)
    values["trusted_senders"] = tuple(values["trusted_senders"])
    return IntakeConfig(**values)


async def upsert_config(db: Database, claims: AuthClaims, *, config: IntakeConfig,
                        page_token: str | None) -> None:
    """Save the config. ``page_token=None`` keeps the stored token; ``""`` clears it.
    The Meta page is routed to this tenant (``PAGE_IN_USE`` if another tenant has it)."""
    _reject_global(claims)
    ciphertext = (_box().encrypt(page_token, aad=str(claims.tenant_id).encode())
                  if page_token else None)
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM meta_page_routes WHERE tenant_id = $1", claims.tenant_id)
        if config.meta_page_id:
            try:
                await conn.execute(
                    "INSERT INTO meta_page_routes (page_id, tenant_id) VALUES ($1, $2)",
                    config.meta_page_id, claims.tenant_id)
            except UniqueViolationError as exc:
                raise ConflictError("That Facebook page is connected to another account.",
                                    code="PAGE_IN_USE") from exc
        await conn.execute(
            "INSERT INTO tenant_lead_intake_configs (tenant_id, enabled, trusted_senders, "
            "form_consent_wording, call_consent_wording, meta_page_id, "
            "meta_page_token_ciphertext, updated_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "ON CONFLICT (tenant_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
            "trusted_senders = EXCLUDED.trusted_senders, "
            "form_consent_wording = EXCLUDED.form_consent_wording, "
            "call_consent_wording = EXCLUDED.call_consent_wording, "
            "meta_page_id = EXCLUDED.meta_page_id, "
            "meta_page_token_ciphertext = CASE WHEN $9 THEN EXCLUDED.meta_page_token_ciphertext "
            "ELSE tenant_lead_intake_configs.meta_page_token_ciphertext END, "
            "updated_by = EXCLUDED.updated_by, updated_at = now()",
            claims.tenant_id, config.enabled, list(config.trusted_senders),
            config.form_consent_wording, config.call_consent_wording, config.meta_page_id,
            ciphertext, claims.subject, page_token is not None,
        )


async def tenant_for_page_system(db: Database, page_id: str) -> str | None:
    value = await db.fetchval("SELECT tenant_id FROM meta_page_routes WHERE page_id = $1",
                              page_id)
    return str(value) if value is not None else None


async def get_page_token(db: Database, claims: AuthClaims) -> str | None:
    _reject_global(claims)
    value = await db.fetchval("SELECT meta_page_token_ciphertext FROM tenant_lead_intake_configs "
                              "WHERE tenant_id = $1", claims.tenant_id)
    return _box().decrypt_str(str(value), aad=str(claims.tenant_id).encode()) if value else None


# -- Intake events (the audit of what arrived + the queue) -----------------------------------

_EVENT_SELECT = ("SELECT intake_id, source, external_id, status, reason, lead_id, fields, "
                 "received_at, resolved_by FROM lead_intake_events ")


async def record_event(db: Database, claims: AuthClaims, *, source: str, external_id: str,
                       status: str, reason: str | None = None,
                       fields: dict[str, Any] | None = None) -> tuple[str, bool]:
    """``(intake_id, created)``. A repeat of (source, external_id) is not created (A10 I2)."""
    _reject_global(claims)
    row = await db.fetchrow(
        "INSERT INTO lead_intake_events (tenant_id, intake_id, source, external_id, status, "
        "reason, fields) VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (tenant_id, source, external_id) DO NOTHING RETURNING intake_id",
        claims.tenant_id, uuid4().hex, source, external_id[:300], status, reason, fields or {})
    if row is not None:
        return str(row["intake_id"]), True
    existing = await db.fetchval(
        "SELECT intake_id FROM lead_intake_events WHERE tenant_id = $1 AND source = $2 "
        "AND external_id = $3", claims.tenant_id, source, external_id[:300])
    return str(existing), False


async def set_event(db: Database, claims: AuthClaims, intake_id: str, *, status: str,
                    reason: str | None = None, lead_id: str | None = None,
                    fields: dict[str, Any] | None = None) -> None:
    _reject_global(claims)
    await db.execute(
        "UPDATE lead_intake_events SET status = $3, reason = $4, lead_id = coalesce($5, lead_id), "
        "fields = coalesce($6, fields) WHERE tenant_id = $1 AND intake_id = $2",
        claims.tenant_id, intake_id, status, reason, lead_id, fields)


async def get_event(db: Database, claims: AuthClaims, intake_id: str) -> IntakeEvent | None:
    _reject_global(claims)
    row = await db.fetchrow(_EVENT_SELECT + "WHERE tenant_id = $1 AND intake_id = $2",
                            claims.tenant_id, intake_id)
    return IntakeEvent(**dict(row)) if row is not None else None


async def list_events(db: Database, claims: AuthClaims, *, queue_only: bool, limit: int,
                      offset: int) -> list[IntakeEvent]:
    _reject_global(claims)
    rows = await db.fetch(
        _EVENT_SELECT + "WHERE tenant_id = $1 AND (NOT $2 OR status IN ('rejected', 'notice')) "
        "ORDER BY received_at DESC LIMIT $3 OFFSET $4",
        claims.tenant_id, queue_only, max(1, min(limit, 200)), max(0, offset))
    return [IntakeEvent(**dict(r)) for r in rows]


async def resolve_event(db: Database, claims: AuthClaims, intake_id: str) -> bool:
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE lead_intake_events SET status = 'resolved', resolved_by = $3, "
        "resolved_at = now() WHERE tenant_id = $1 AND intake_id = $2 "
        "AND status IN ('rejected', 'notice')", claims.tenant_id, intake_id, claims.subject)
    return str(tag).endswith(" 1")
