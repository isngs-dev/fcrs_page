"""Call config, sessions and transcript turns (migration 0071).

Tenant methods take ``AuthClaims``. ``*_by_id_system`` reads serve Plivo
webhooks: the call id comes from our HMAC ticket, and the row's ``tenant_id``
then decides whose auth token verifies the signature and whose claims are used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.crypto import SecretBox
from common.db import Database
from common.errors import ConflictError, ValidationError

from api.agents.repository import _reject_global
from api.config import get_api_settings


@dataclass(frozen=True)
class CallConfig:
    enabled: bool
    plivo_auth_id: str
    from_number: str
    transfer_number: str | None
    company_name: str
    voice: str
    voice_model: str | None
    voicemail_policy: str
    voicemail_message: str | None
    max_call_seconds: int
    max_concurrent_calls: int
    recording_enabled: bool
    inbound_enabled: bool = True
    recording_retention_days: int = 90  # A9 H8


_CONFIG_COLUMNS = (
    "SELECT enabled, plivo_auth_id, from_number, transfer_number, company_name, voice, "
    "voice_model, voicemail_policy, voicemail_message, max_call_seconds, max_concurrent_calls, "
    "recording_enabled, inbound_enabled, recording_retention_days "
    "FROM tenant_call_agent_configs WHERE tenant_id = $1"
)


def _box() -> SecretBox:
    return SecretBox(get_api_settings().secret_encryption_key)


def validate_voicemail(message: str, company: str) -> None:
    """A left voicemail must disclose the AI, name the company and give a callback number."""
    if not re.search(r"\bAI\b|artificial", message, re.IGNORECASE):
        raise ValidationError("The voicemail must say it's an AI assistant.",
                              code="VOICEMAIL_NEEDS_AI_DISCLOSURE")
    if company.lower() not in message.lower():
        raise ValidationError("The voicemail must name the company.",
                              code="VOICEMAIL_NEEDS_COMPANY")
    if not re.search(r"\d[\d\s().-]{8,}\d", message):
        raise ValidationError("The voicemail must include a callback number.",
                              code="VOICEMAIL_NEEDS_CALLBACK_NUMBER")


async def get_call_config(db: Database, claims: AuthClaims) -> CallConfig | None:
    _reject_global(claims)
    row = await db.fetchrow(_CONFIG_COLUMNS, claims.tenant_id)
    return CallConfig(**dict(row)) if row is not None else None


async def get_auth_token(db: Database, tenant_id: str) -> str | None:
    """The tenant's decrypted Plivo auth token (system read: webhook verification)."""
    ciphertext = await db.fetchval(
        "SELECT plivo_auth_token_ciphertext FROM tenant_call_agent_configs WHERE tenant_id = $1",
        tenant_id,
    )
    if ciphertext is None:
        return None
    return _box().decrypt_str(str(ciphertext), aad=tenant_id.encode())


async def upsert_call_config(db: Database, claims: AuthClaims, *, values: dict[str, Any],
                             auth_token: str | None) -> None:
    """Full upsert; ``auth_token=None`` keeps the stored token (required on first save)."""
    _reject_global(claims)
    ciphertext = (_box().encrypt(auth_token, aad=str(claims.tenant_id).encode())
                  if auth_token else None)
    tag = await db.execute(
        "INSERT INTO tenant_call_agent_configs (tenant_id, enabled, plivo_auth_id, "
        "plivo_auth_token_ciphertext, from_number, transfer_number, company_name, voice, "
        "voice_model, voicemail_policy, voicemail_message, max_call_seconds, "
        "max_concurrent_calls, recording_enabled, updated_by, inbound_enabled, "
        "recording_retention_days) "
        # Keep the stored token when none is sent (the NOT NULL check runs on the
        # proposed row, before ON CONFLICT -- so coalesce it here, not only below).
        "SELECT $1, $2, $3, coalesce($4, (SELECT plivo_auth_token_ciphertext FROM "
        "tenant_call_agent_configs WHERE tenant_id = $1)), $5, $6, $7, $8, $9, $10, $11, $12, "
        "$13, $14, $15, $16, $17 "
        "WHERE $4::text IS NOT NULL OR EXISTS (SELECT 1 FROM tenant_call_agent_configs "
        "WHERE tenant_id = $1) "
        "ON CONFLICT (tenant_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "plivo_auth_id = EXCLUDED.plivo_auth_id, "
        "plivo_auth_token_ciphertext = coalesce($4, tenant_call_agent_configs"
        ".plivo_auth_token_ciphertext), "
        "from_number = EXCLUDED.from_number, transfer_number = EXCLUDED.transfer_number, "
        "company_name = EXCLUDED.company_name, voice = EXCLUDED.voice, "
        "voice_model = EXCLUDED.voice_model, voicemail_policy = EXCLUDED.voicemail_policy, "
        "voicemail_message = EXCLUDED.voicemail_message, "
        "max_call_seconds = EXCLUDED.max_call_seconds, "
        "max_concurrent_calls = EXCLUDED.max_concurrent_calls, "
        "recording_enabled = EXCLUDED.recording_enabled, updated_by = EXCLUDED.updated_by, "
        "inbound_enabled = EXCLUDED.inbound_enabled, "
        "recording_retention_days = EXCLUDED.recording_retention_days, updated_at = now()",
        claims.tenant_id, values["enabled"], values["plivo_auth_id"], ciphertext,
        values["from_number"], values["transfer_number"], values["company_name"],
        values["voice"], values["voice_model"], values["voicemail_policy"],
        values["voicemail_message"], values["max_call_seconds"],
        values["max_concurrent_calls"], values["recording_enabled"], claims.subject,
        values.get("inbound_enabled", True), values.get("recording_retention_days", 90),
    )
    if str(tag).endswith(" 0"):
        raise ValidationError("The Plivo auth token is required.", code="CALL_CONFIG_INVALID")


# -- Sessions --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSessionRow:
    tenant_id: str
    call_id: str
    direction: str
    task_id: str | None
    plan_id: str | None
    contact_id: str | None
    from_e164: str
    to_e164: str
    provider_call_uuid: str | None
    status: str
    outcome: str | None
    recording_consent: bool
    recording_id: str | None
    session: dict[str, Any]
    created_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None


_SESSION_SELECT = (
    "SELECT tenant_id, call_id, direction, task_id, plan_id, contact_id, from_e164, to_e164, "
    "provider_call_uuid, status, outcome, recording_consent, recording_id, session, created_at, "
    "answered_at, ended_at FROM call_sessions "
)


async def create_session(db: Database, claims: AuthClaims, *, task_id: str, plan_id: str,
                         contact_id: str, from_e164: str, to_e164: str, limit: int) -> str | None:
    """A new outbound session, or ``None`` when the tenant already has ``limit`` live
    calls (A9 H5: the count and the insert happen under one per-tenant lock, so
    parallel dispatchers can't overshoot the cap)."""
    _reject_global(claims)
    call_id = uuid4().hex
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                           f"call_sessions:{claims.tenant_id}")
        live = await conn.fetchval(
            "SELECT count(*) FROM call_sessions WHERE tenant_id = $1 "
            "AND status IN ('dialing', 'in_progress', 'transferring')", claims.tenant_id)
        if live >= limit:
            return None
        await conn.execute(
            "INSERT INTO call_sessions (tenant_id, call_id, direction, task_id, plan_id, "
            "contact_id, from_e164, to_e164) VALUES ($1, $2, 'outbound', $3, $4, $5, $6, $7)",
            claims.tenant_id, call_id, task_id, plan_id, contact_id, from_e164, to_e164,
        )
    return call_id


async def get_session_system(db: Database, call_id: str) -> CallSessionRow | None:
    """By call id only -- callers must have verified our HMAC ticket for ``call_id``."""
    row = await db.fetchrow(_SESSION_SELECT + "WHERE call_id = $1", call_id)
    return CallSessionRow(**dict(row)) if row is not None else None


async def get_session(db: Database, claims: AuthClaims, call_id: str) -> CallSessionRow | None:
    _reject_global(claims)
    row = await db.fetchrow(_SESSION_SELECT + "WHERE tenant_id = $1 AND call_id = $2",
                            claims.tenant_id, call_id)
    return CallSessionRow(**dict(row)) if row is not None else None


async def update_session(db: Database, claims: AuthClaims, call_id: str, **fields: Any) -> None:
    """Set the given columns (names are code constants, never input)."""
    _reject_global(claims)
    allowed = {"status", "outcome", "amd_result", "recording_consent", "recording_id",
               "recording_url", "transfer_reason", "summary", "sentiment", "session",
               "provider_request_uuid", "provider_call_uuid", "answered_at", "ended_at",
               "duration_s"}
    assert set(fields) <= allowed, set(fields) - allowed  # noqa: S101 -- programming error
    sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(fields, start=3))
    await db.execute(
        f"UPDATE call_sessions SET {sets} WHERE tenant_id = $1 AND call_id = $2",  # noqa: S608
        claims.tenant_id, call_id, *fields.values(),
    )


async def count_active(db: Database, claims: AuthClaims) -> int:
    _reject_global(claims)
    return int(await db.fetchval(
        "SELECT count(*) FROM call_sessions WHERE tenant_id = $1 "
        "AND status IN ('dialing', 'in_progress', 'transferring')", claims.tenant_id))


async def add_turn(db: Database, claims: AuthClaims, call_id: str, *, speaker: str, text: str,
                   latency_ms: int | None = None) -> None:
    _reject_global(claims)
    await db.execute(
        "INSERT INTO call_transcript_turns (tenant_id, call_id, turn_index, speaker, text, "
        "latency_ms) SELECT $1, $2, coalesce(max(turn_index), 0) + 1, $3, $4, $5 "
        "FROM call_transcript_turns WHERE tenant_id = $1 AND call_id = $2",
        claims.tenant_id, call_id, speaker, text[:4000], latency_ms,
    )


async def list_turns(db: Database, claims: AuthClaims, call_id: str) -> list[dict[str, Any]]:
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT turn_index, speaker, text, latency_ms, created_at FROM call_transcript_turns "
        "WHERE tenant_id = $1 AND call_id = $2 ORDER BY turn_index",
        claims.tenant_id, call_id,
    )
    return [dict(r) for r in rows]


async def list_sessions(db: Database, claims: AuthClaims, *, status: str | None, limit: int,
                        offset: int, contact_id: str | None = None,
                        call_id: str | None = None) -> list[dict[str, Any]]:
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT call_id, direction, contact_id, from_e164, to_e164, status, outcome, amd_result, "
        "recording_consent, recording_url IS NOT NULL AS has_recording, transfer_reason, summary, "
        "sentiment, created_at, answered_at, ended_at, duration_s "
        "FROM call_sessions WHERE tenant_id = $1 AND ($2::text IS NULL OR status = $2) "
        "AND ($5::text IS NULL OR contact_id = $5) AND ($6::text IS NULL OR call_id = $6) "
        "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
        claims.tenant_id, status, max(1, min(limit, 200)), max(0, offset), contact_id, call_id,
    )
    return [dict(r) for r in rows]


async def get_recording_url(db: Database, claims: AuthClaims, call_id: str) -> str | None:
    _reject_global(claims)
    value = await db.fetchval(
        "SELECT recording_url FROM call_sessions WHERE tenant_id = $1 AND call_id = $2",
        claims.tenant_id, call_id)
    return str(value) if value else None


async def counts_since(db: Database, claims: AuthClaims, since: datetime) -> dict[str, int]:
    """Calls placed/received and calls that booked a meeting since ``since`` (A8 overview)."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT count(*) AS calls, count(*) FILTER (WHERE outcome = 'booked') AS booked "
        "FROM call_sessions WHERE tenant_id = $1 AND created_at >= $2",
        claims.tenant_id, since)
    assert row is not None  # noqa: S101 -- an aggregate always returns a row
    return {"calls": int(row["calls"]), "booked": int(row["booked"])}


async def latency_stats(db: Database, claims: AuthClaims, *, last: int = 500) -> dict[str, Any]:
    """p50/p95 of Rebecca's turn latency over the last ``last`` measured turns (L12)."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT count(*) AS turns, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 FROM ("
        "  SELECT latency_ms FROM call_transcript_turns WHERE tenant_id = $1 "
        "  AND latency_ms IS NOT NULL ORDER BY created_at DESC LIMIT $2) t",
        claims.tenant_id, last,
    )
    assert row is not None  # noqa: S101 -- an aggregate always returns a row
    return {"turns": int(row["turns"]),
            "p50_ms": round(row["p50"]) if row["p50"] is not None else None,
            "p95_ms": round(row["p95"]) if row["p95"] is not None else None}


async def stale_sessions_system(db: Database, *, grace_seconds: int) -> list[dict[str, Any]]:
    """Sessions still dialing/in progress past their tenant's max call time + grace (L8)."""
    rows = await db.fetch(
        "UPDATE call_sessions s SET status = 'failed', outcome = coalesce(s.outcome, 'failed'), "
        "ended_at = now() FROM tenant_call_agent_configs c "
        "WHERE c.tenant_id = s.tenant_id AND s.status IN ('dialing', 'in_progress', "
        "'transferring') AND s.created_at < now() - make_interval(secs => c.max_call_seconds "
        "+ $1) RETURNING s.tenant_id, s.call_id, s.task_id",
        grace_seconds,
    )
    return [dict(r) for r in rows]


# -- Number registry (A7 N1) ------------------------------------------------------------


async def register_number(db: Database, claims: AuthClaims, number: str) -> None:
    """Route ``number`` to the caller's tenant (releasing its previous number).
    A number another tenant holds -> ``ConflictError(NUMBER_IN_USE)``."""
    _reject_global(claims)
    owner = await db.fetchval(
        "INSERT INTO telephony_numbers (number_e164, tenant_id) VALUES ($1, $2) "
        "ON CONFLICT (number_e164) DO UPDATE SET active = true "
        "WHERE telephony_numbers.tenant_id = EXCLUDED.tenant_id RETURNING tenant_id",
        number, claims.tenant_id,
    )
    if owner is None:
        raise ConflictError("That number is already used by another workspace.",
                            code="NUMBER_IN_USE")
    await db.execute("DELETE FROM telephony_numbers WHERE tenant_id = $1 AND number_e164 <> $2",
                     claims.tenant_id, number)


async def tenant_for_number_system(db: Database, number: str) -> str | None:
    """The tenant an inbound call to ``number`` belongs to -- the only claims-less
    inbound read; the caller still verifies Plivo's signature with that tenant's token."""
    value = await db.fetchval(
        "SELECT tenant_id FROM telephony_numbers WHERE number_e164 = $1 AND active", number)
    return str(value) if value is not None else None


async def create_inbound_session(db: Database, claims: AuthClaims, *, from_e164: str,
                                 to_e164: str, contact_id: str | None, call_uuid: str | None,
                                 ) -> str:
    _reject_global(claims)
    call_id = uuid4().hex
    await db.execute(
        "INSERT INTO call_sessions (tenant_id, call_id, direction, contact_id, from_e164, "
        "to_e164, provider_call_uuid, status, answered_at) "
        "VALUES ($1, $2, 'inbound', $3, $4, $5, $6, 'in_progress', now())",
        claims.tenant_id, call_id, contact_id, from_e164, to_e164, call_uuid,
    )
    return call_id


async def session_by_call_uuid(db: Database, claims: AuthClaims, call_uuid: str
                               ) -> CallSessionRow | None:
    _reject_global(claims)
    row = await db.fetchrow(_SESSION_SELECT + "WHERE tenant_id = $1 AND provider_call_uuid = $2",
                            claims.tenant_id, call_uuid)
    return CallSessionRow(**dict(row)) if row is not None else None


# -- Recording retention + a contact's data (A9 H8/H9) ------------------------------------


async def expired_recordings_system(db: Database, *, limit: int = 200) -> list[dict[str, Any]]:
    """Recordings older than their tenant's retention (system-scoped, Beat-driven)."""
    rows = await db.fetch(
        "SELECT s.tenant_id, s.call_id, s.recording_id FROM call_sessions s "
        "JOIN tenant_call_agent_configs c USING (tenant_id) "
        "WHERE s.recording_url IS NOT NULL "
        "AND s.created_at < now() - make_interval(days => c.recording_retention_days) "
        "ORDER BY s.created_at LIMIT $1", limit)
    return [dict(r) for r in rows]


async def contact_recordings(db: Database, claims: AuthClaims, contact_id: str
                             ) -> list[dict[str, Any]]:
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT call_id, recording_id FROM call_sessions WHERE tenant_id = $1 "
        "AND contact_id = $2 AND recording_url IS NOT NULL", claims.tenant_id, contact_id)
    return [dict(r) for r in rows]


async def delete_for_contact(db: Database, claims: AuthClaims, contact_id: str) -> int:
    """Delete the contact's calls (transcripts cascade). Joins the caller's transaction."""
    _reject_global(claims)
    rows = await db.fetch("DELETE FROM call_sessions WHERE tenant_id = $1 AND contact_id = $2 "
                          "RETURNING call_id", claims.tenant_id, contact_id)
    return len(rows)
