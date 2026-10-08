"""AI voice confirmation calls -- config + per-booking call rows (migration 0062).

Same shape as ``api.calls.repository``: claims-scoped reads/writes for the
admin surface and the booking route, plus claims-less reads keyed by the raw
``tenant_id`` for the Celery task and the Twilio webhooks (which have no
session -- the Twilio signature, verified by the caller, is their auth).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

# Defaults for a chatbot that turns the feature on without editing questions.
# {date}/{time} are filled with the booked slot (see api.calls.voice).
DEFAULT_QUESTIONS: list[str] = [
    "Can you confirm you're available for your call on {date} at {time}?",
    "Is this about an existing problem, such as a leak or damage?",
    "Would you like us to send you a reminder before the call?",
]


@dataclass(frozen=True)
class VoiceCallConfig:
    enabled: bool
    questions: list[str]


@dataclass(frozen=True)
class VoiceCall:
    call_id: str
    tenant_id: str
    event_id: str
    lead_id: str | None
    to_number: str
    questions: list[str]
    status: str
    twilio_call_sid: str | None
    transcript: list[dict[str, Any]]  # ordered by question index
    last_error: str | None
    created_at: Any


def _reject_global(claims: AuthClaims) -> None:
    if claims.tenant_id is None:
        raise ValidationError(
            "Voice call repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_voice_call_config(db: Database, claims: AuthClaims) -> VoiceCallConfig | None:
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT enabled, questions FROM tenant_voice_call_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if row is None:
        return None
    return VoiceCallConfig(enabled=bool(row["enabled"]), questions=list(row["questions"]))


async def upsert_voice_call_config(
    db: Database, claims: AuthClaims, *, enabled: bool, questions: list[str],
) -> None:
    _reject_global(claims)
    await db.execute(
        "INSERT INTO tenant_voice_call_configs (tenant_id, enabled, questions) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "enabled = $2, questions = $3, updated_at = now()",
        claims.tenant_id,
        enabled,
        questions,
    )


async def create_voice_call(
    db: Database,
    claims: AuthClaims,
    *,
    event_id: str,
    lead_id: str | None,
    to_number: str,
    questions: list[str],
) -> str | None:
    """Create the call row for a booking. ``None`` if this booking already has one."""
    _reject_global(claims)
    row = await db.fetchrow(
        "INSERT INTO voice_calls (call_id, tenant_id, event_id, lead_id, to_number, questions) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (tenant_id, event_id) DO NOTHING RETURNING call_id",
        uuid4().hex,
        claims.tenant_id,
        event_id,
        lead_id,
        to_number,
        questions,
    )
    return None if row is None else str(row["call_id"])


_CALL_COLUMNS = (
    "call_id, tenant_id, event_id, lead_id, to_number, questions, status, "
    "twilio_call_sid, transcript, last_error, created_at"
)


def _row_to_call(row: Any) -> VoiceCall:
    raw = row["transcript"] or {}
    transcript = [raw[k] for k in sorted(raw, key=int)]
    return VoiceCall(
        call_id=str(row["call_id"]),
        tenant_id=str(row["tenant_id"]),
        event_id=str(row["event_id"]),
        lead_id=None if row["lead_id"] is None else str(row["lead_id"]),
        to_number=str(row["to_number"]),
        questions=list(row["questions"]),
        status=str(row["status"]),
        twilio_call_sid=None if row["twilio_call_sid"] is None else str(row["twilio_call_sid"]),
        transcript=transcript,
        last_error=None if row["last_error"] is None else str(row["last_error"]),
        created_at=row["created_at"],
    )


async def get_voice_call_by_id(db: Database, tenant_id: str, call_id: str) -> VoiceCall | None:
    """Claims-less read for the Celery task / Twilio webhooks (see module docstring)."""
    row = await db.fetchrow(
        f"SELECT {_CALL_COLUMNS} FROM voice_calls WHERE tenant_id = $1 AND call_id = $2",  # noqa: S608
        tenant_id,
        call_id,
    )
    return None if row is None else _row_to_call(row)


async def list_voice_calls_for_lead(
    db: Database, claims: AuthClaims, lead_id: str,
) -> list[VoiceCall]:
    _reject_global(claims)
    rows = await db.fetch(
        f"SELECT {_CALL_COLUMNS} FROM voice_calls "  # noqa: S608
        "WHERE tenant_id = $1 AND lead_id = $2 ORDER BY created_at DESC",
        claims.tenant_id,
        lead_id,
    )
    return [_row_to_call(row) for row in rows]


async def set_voice_call_status(
    db: Database,
    tenant_id: str,
    call_id: str,
    *,
    status: str,
    twilio_call_sid: str | None = None,
    last_error: str | None = None,
) -> None:
    await db.execute(
        "UPDATE voice_calls SET status = $3, "
        "twilio_call_sid = COALESCE($4, twilio_call_sid), "
        "last_error = COALESCE($5, last_error), updated_at = now() "
        "WHERE tenant_id = $1 AND call_id = $2",
        tenant_id,
        call_id,
        status,
        twilio_call_sid,
        last_error,
    )


async def record_voice_call_answer(
    db: Database, tenant_id: str, call_id: str, *, index: int, entry: dict[str, Any],
) -> None:
    """Store the answer for question ``index`` (a webhook retry overwrites, never duplicates)."""
    await db.execute(
        "UPDATE voice_calls SET "
        "transcript = jsonb_set(transcript, ARRAY[$3::text], $4::jsonb, true), "
        "status = CASE WHEN status IN ('queued', 'calling') THEN 'in_progress' ELSE status END, "
        "updated_at = now() "
        "WHERE tenant_id = $1 AND call_id = $2",
        tenant_id,
        call_id,
        str(index),
        entry,
    )


async def get_voice_call_enabled_by_tenant_id(db: Database, tenant_id: str) -> bool:
    """Claims-less flag for the widget's pre-auth session mint (gateway): when
    on, the booking card requires a phone and its consent covers the call."""
    row = await db.fetchrow(
        "SELECT enabled FROM tenant_voice_call_configs WHERE tenant_id = $1", tenant_id,
    )
    return bool(row["enabled"]) if row is not None else False
