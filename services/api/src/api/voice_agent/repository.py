"""AI voice agent -- per-tenant config + per-call rows (migration 0063).

Claims-scoped reads/writes for the admin console, plus claims-less reads keyed
by the raw ``call_id`` (or Plivo ``CallUUID``) for the Plivo webhooks. Those
have no visitor session: the Plivo signature plus the HMAC call ticket
(``api.voice_agent.agent``) prove which call row they belong to, and the row
carries the tenant.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

DEFAULT_TRANSFER_NUMBER = "+17868233553"


@dataclass(frozen=True)
class VoiceAgentConfig:
    enabled: bool = False
    greeting: str | None = None
    transfer_number: str = DEFAULT_TRANSFER_NUMBER
    timezone: str = "America/New_York"
    open_time: str = "09:00"  # HH:MM, in `timezone`
    close_time: str = "18:00"
    open_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday = 0
    max_minutes: int = 10


@dataclass(frozen=True)
class VoiceAgentCall:
    call_id: str
    tenant_id: str
    visitor_id: str
    conversation_id: str
    status: str
    transfer_reason: str | None
    started_at: datetime | None
    ended_at: datetime | None
    session: dict[str, Any] | None = None  # CallSession.to_state() between turns


def _reject_global(claims: AuthClaims) -> None:
    if claims.tenant_id is None:
        raise ValidationError(
            "Voice agent repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


_CONFIG_COLUMNS = (
    "enabled, greeting, transfer_number, timezone, open_time, close_time, open_days, max_minutes"
)


async def get_voice_agent_config_by_tenant_id(db: Database, tenant_id: str) -> VoiceAgentConfig:
    """The tenant's config, or the defaults (feature off) when it has none."""
    row = await db.fetchrow(
        f"SELECT {_CONFIG_COLUMNS} FROM tenant_voice_agent_configs WHERE tenant_id = $1",  # noqa: S608
        tenant_id,
    )
    if row is None:
        return VoiceAgentConfig()
    return VoiceAgentConfig(
        enabled=bool(row["enabled"]),
        greeting=row["greeting"],
        transfer_number=str(row["transfer_number"]),
        timezone=str(row["timezone"]),
        open_time=str(row["open_time"]),
        close_time=str(row["close_time"]),
        open_days=tuple(int(d) for d in row["open_days"]),
        max_minutes=int(row["max_minutes"]),
    )


async def get_voice_agent_config(db: Database, claims: AuthClaims) -> VoiceAgentConfig:
    _reject_global(claims)
    return await get_voice_agent_config_by_tenant_id(db, str(claims.tenant_id))


async def upsert_voice_agent_config(
    db: Database, claims: AuthClaims, config: VoiceAgentConfig,
) -> None:
    _reject_global(claims)
    await db.execute(
        "INSERT INTO tenant_voice_agent_configs "  # noqa: S608
        f"(tenant_id, {_CONFIG_COLUMNS}) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
        "ON CONFLICT (tenant_id) DO UPDATE SET enabled = $2, greeting = $3, "
        "transfer_number = $4, timezone = $5, open_time = $6, close_time = $7, "
        "open_days = $8, max_minutes = $9, updated_at = now()",
        claims.tenant_id,
        config.enabled,
        config.greeting,
        config.transfer_number,
        config.timezone,
        config.open_time,
        config.close_time,
        list(config.open_days),
        config.max_minutes,
    )


async def create_voice_agent_call(
    db: Database, claims: AuthClaims, *, conversation_id: str,
) -> str:
    _reject_global(claims)
    call_id = uuid4().hex
    await db.execute(
        "INSERT INTO voice_agent_calls (call_id, tenant_id, visitor_id, conversation_id) "
        "VALUES ($1, $2, $3, $4)",
        call_id,
        claims.tenant_id,
        claims.subject,
        conversation_id,
    )
    return call_id


_CALL_COLUMNS = (
    "call_id, tenant_id, visitor_id, conversation_id, status, transfer_reason, "
    "started_at, ended_at, session"
)


async def get_voice_agent_call(db: Database, call_id: str) -> VoiceAgentCall | None:
    """Claims-less read -- callers must have verified the call ticket first."""
    row = await db.fetchrow(
        f"SELECT {_CALL_COLUMNS} FROM voice_agent_calls WHERE call_id = $1",  # noqa: S608
        call_id,
    )
    return _call(row)


async def get_voice_agent_call_by_plivo_uuid(db: Database, call_uuid: str) -> VoiceAgentCall | None:
    """Claims-less read -- callers must have verified the Plivo signature first."""
    row = await db.fetchrow(
        f"SELECT {_CALL_COLUMNS} FROM voice_agent_calls WHERE plivo_call_uuid = $1",  # noqa: S608
        call_uuid,
    )
    return _call(row)


def _call(row: Any) -> VoiceAgentCall | None:
    if row is None:
        return None
    return VoiceAgentCall(
        call_id=str(row["call_id"]),
        tenant_id=str(row["tenant_id"]),
        visitor_id=str(row["visitor_id"]),
        conversation_id=str(row["conversation_id"]),
        status=str(row["status"]),
        transfer_reason=row["transfer_reason"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        session=row["session"],
    )


async def update_voice_agent_call(
    db: Database,
    call_id: str,
    *,
    status: str,
    transfer_reason: str | None = None,
    plivo_call_uuid: str | None = None,
    started: bool = False,
    ended: bool = False,
) -> None:
    await db.execute(
        "UPDATE voice_agent_calls SET status = $2, "
        "transfer_reason = COALESCE($3, transfer_reason), "
        "plivo_call_uuid = COALESCE($4, plivo_call_uuid), "
        "started_at = CASE WHEN $5 THEN COALESCE(started_at, now()) ELSE started_at END, "
        "ended_at = CASE WHEN $6 THEN COALESCE(ended_at, now()) ELSE ended_at END, "
        "updated_at = now() WHERE call_id = $1",
        call_id,
        status,
        transfer_reason,
        plivo_call_uuid,
        started,
        ended,
    )


async def save_voice_agent_session(db: Database, call_id: str, session: dict[str, Any]) -> None:
    await db.execute(
        "UPDATE voice_agent_calls SET session = $2, updated_at = now() WHERE call_id = $1",
        call_id,
        session,
    )


async def list_voice_call_conversation_ids(
    db: Database, claims: AuthClaims, conversation_ids: list[str],
) -> set[str]:
    """Which of ``conversation_ids`` include a voice call (the admin's badge)."""
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT DISTINCT conversation_id FROM voice_agent_calls "
        "WHERE tenant_id = $1 AND conversation_id = ANY($2::text[])",
        claims.tenant_id,
        conversation_ids,
    )
    return {str(row["conversation_id"]) for row in rows}
