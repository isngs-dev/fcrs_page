"""Qualification storage, tenant-scoped (plan 1.2/1.3). Raw parameterized SQL only."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.qualification.scoring import DEFAULT_CONFIG, Band, QualConfig

# What "spoke with Rebecca" excludes: she never got them on the line.
_NOT_SPOKEN = ("no_answer", "voicemail", "failed", "wrong_number")


def _reject_global(claims: AuthClaims) -> None:
    if claims.tenant_id is None:
        raise ValidationError("Qualification is tenant-scoped.",
                              code="GLOBAL_CALLER_NOT_PERMITTED")


@dataclass(frozen=True)
class Record:
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    score: int | None = None
    band: Band | None = None
    scored_at: datetime | None = None
    alerted_at: datetime | None = None

    @property
    def chosen(self) -> dict[str, str]:
        return {key: str(a["choice"]) for key, a in self.answers.items()}


async def get_config(db: Database, claims: AuthClaims) -> QualConfig:
    _reject_global(claims)
    row = await db.fetchrow("SELECT weights, questions FROM tenant_qualification_configs "
                            "WHERE tenant_id = $1", claims.tenant_id)
    if row is None:
        return DEFAULT_CONFIG
    return QualConfig.model_validate({"weights": _json(row["weights"]),
                                      "questions": _json(row["questions"])})


async def save_config(db: Database, claims: AuthClaims, config: QualConfig) -> None:
    _reject_global(claims)
    body = config.model_dump()
    await db.execute(
        "INSERT INTO tenant_qualification_configs (tenant_id, weights, questions) "
        "VALUES ($1, $2::jsonb, $3::jsonb) ON CONFLICT (tenant_id) DO UPDATE SET "
        "weights = EXCLUDED.weights, questions = EXCLUDED.questions, updated_at = now()",
        claims.tenant_id, body["weights"], body["questions"])


async def get_record(db: Database, claims: AuthClaims, contact_id: str) -> Record:
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT answers, score, band, scored_at, alerted_at FROM contact_qualifications "
        "WHERE tenant_id = $1 AND contact_id = $2", claims.tenant_id, contact_id)
    if row is None:
        return Record()
    return Record(answers=dict(_json(row["answers"]) or {}), score=row["score"],
                  band=row["band"], scored_at=row["scored_at"], alerted_at=row["alerted_at"])


async def save_answer(db: Database, claims: AuthClaims, contact_id: str, key: str, *,
                      choice: str, heard: str, channel: str, at: datetime) -> None:
    _reject_global(claims)
    answer = {key: {"choice": choice, "heard": heard[:500], "channel": channel,
                    "at": at.isoformat()}}
    await db.execute(
        "INSERT INTO contact_qualifications (tenant_id, contact_id, answers) "
        "VALUES ($1, $2, $3::jsonb) ON CONFLICT (tenant_id, contact_id) DO UPDATE SET "
        "answers = contact_qualifications.answers || EXCLUDED.answers",
        claims.tenant_id, contact_id, answer)


async def save_score(db: Database, claims: AuthClaims, contact_id: str, *, score: int,
                     band: Band) -> None:
    _reject_global(claims)
    await db.execute(
        "INSERT INTO contact_qualifications (tenant_id, contact_id, score, band, scored_at) "
        "VALUES ($1, $2, $3, $4, now()) ON CONFLICT (tenant_id, contact_id) DO UPDATE SET "
        "score = EXCLUDED.score, band = EXCLUDED.band, scored_at = EXCLUDED.scored_at",
        claims.tenant_id, contact_id, score, band)


async def mark_alerted(db: Database, claims: AuthClaims, contact_id: str) -> bool:
    """True exactly once per contact: the hot-lead alert is sent by whoever wins this."""
    _reject_global(claims)
    row = await db.fetchrow(
        "UPDATE contact_qualifications SET alerted_at = now() WHERE tenant_id = $1 "
        "AND contact_id = $2 AND alerted_at IS NULL RETURNING contact_id",
        claims.tenant_id, contact_id)
    return row is not None


async def engagement_signals(db: Database, claims: AuthClaims, contact_id: str
                             ) -> dict[str, bool]:
    """What the lead did, read from the calls, emails, chat identities and consents."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT "
        " EXISTS (SELECT 1 FROM call_sessions WHERE tenant_id = $1 AND contact_id = $2 "
        "         AND outcome IS NOT NULL AND outcome <> ALL($3::text[])) AS spoke, "
        " EXISTS (SELECT 1 FROM email_messages m JOIN email_threads t "
        "         ON t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id "
        "         WHERE m.tenant_id = $1 AND t.contact_id = $2 AND m.direction = 'inbound') "
        "   AS replied, "
        " EXISTS (SELECT 1 FROM contact_identities WHERE tenant_id = $1 AND contact_id = $2 "
        "         AND identity_type = 'visitor_id') AS chatted, "
        " EXISTS (SELECT 1 FROM contact_consents WHERE tenant_id = $1 AND contact_id = $2 "
        "         AND channel = 'call' AND status = 'granted') AS asked_for_call",
        claims.tenant_id, contact_id, list(_NOT_SPOKEN))
    if row is None:  # a SELECT of EXISTS always returns one row
        raise RuntimeError("engagement_signals returned no row")
    return {k: bool(row[k]) for k in ("spoke", "replied", "chatted", "asked_for_call")}


async def move(db: Database, claims: AuthClaims, source: str, target: str) -> None:
    """Fold ``source``'s answers into ``target`` (target's own answers win) -- plan 1.1."""
    _reject_global(claims)
    await db.execute(
        "INSERT INTO contact_qualifications (tenant_id, contact_id, answers) "
        "SELECT tenant_id, $3, answers FROM contact_qualifications "
        "WHERE tenant_id = $1 AND contact_id = $2 "
        "ON CONFLICT (tenant_id, contact_id) DO UPDATE SET "
        "answers = EXCLUDED.answers || contact_qualifications.answers",
        claims.tenant_id, source, target)
    await db.execute("DELETE FROM contact_qualifications WHERE tenant_id = $1 "
                     "AND contact_id = $2", claims.tenant_id, source)


async def delete_for_contact(db: Database, claims: AuthClaims, contact_id: str) -> None:
    _reject_global(claims)
    await db.execute("DELETE FROM contact_qualifications WHERE tenant_id = $1 "
                     "AND contact_id = $2", claims.tenant_id, contact_id)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value
