"""Transactional outbox (``agent_events``).

``emit`` must run on the SAME connection/transaction as the caller's state
change, so an event exists iff that change committed. Dispatch is
system-scoped (``claim_due_events``); handlers get claims rebuilt from the
row's ``tenant_id``.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError
from common.logging import _correlation_id

from api.agents.repository import _reject_global

KNOWN_KINDS = frozenset({
    "inbound_lead_created", "outreach_requested", "email_reply_received", "email_bounced",
    "email_complained", "call_completed", "meeting_booked", "opt_out_received", "human_takeover",
    "intake_meta_lead",
})
# Kinds with a handler. Emitting any other known kind would pile up unprocessed,
# so it is refused until the sprint that adds its handler enables it.
ENABLED_KINDS = frozenset({
    "outreach_requested", "opt_out_received", "human_takeover", "meeting_booked",
    "email_reply_received", "email_bounced", "email_complained", "inbound_lead_created",
    "call_completed", "intake_meta_lead",
})
MAX_EVENT_ATTEMPTS = 5


async def emit(
    conn: Database,
    claims: AuthClaims,
    kind: str,
    payload: dict[str, Any],
    *,
    available_at: datetime | None = None,
) -> str:
    """Write an outbox row on ``conn`` (pass the caller's transaction connection)."""
    _reject_global(claims)
    if kind not in KNOWN_KINDS:
        raise ValidationError(f"Unknown agent event kind '{kind}'.", code="UNKNOWN_EVENT_KIND")
    if kind not in ENABLED_KINDS:
        raise ValidationError(
            f"Agent event kind '{kind}' is not enabled yet.", code="EVENT_KIND_NOT_ENABLED",
        )
    event_id = uuid4().hex
    await conn.execute(
        "INSERT INTO agent_events (tenant_id, event_id, kind, payload, available_at, "
        "correlation_id) VALUES ($1, $2, $3, $4, coalesce($5, now()), $6)",
        claims.tenant_id, event_id, kind, payload, available_at, _correlation_id.get(),
    )
    return event_id


async def get_event_status(
    db: Database, claims: AuthClaims, event_id: str,
) -> dict[str, Any] | None:
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT event_id, kind, status, attempts, last_error, created_at, processed_at "
        "FROM agent_events WHERE tenant_id = $1 AND event_id = $2",
        claims.tenant_id, event_id,
    )
    return dict(row) if row is not None else None


@dataclass(frozen=True)
class ClaimedEvent:
    tenant_id: str
    event_id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    correlation_id: str | None


async def claim_due_events(db: Database, *, limit: int) -> list[ClaimedEvent]:
    """System-scoped exactly-once claim: ``pending`` + due -> ``processing``, attempts+1."""
    rows = await db.fetch(
        "UPDATE agent_events SET status = 'processing', attempts = attempts + 1, "
        "processed_at = now() "
        "WHERE (tenant_id, event_id) IN ("
        "  SELECT tenant_id, event_id FROM agent_events "
        "  WHERE status = 'pending' AND available_at <= now() "
        "  ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT $1) "
        "RETURNING tenant_id, event_id, kind, payload, attempts, correlation_id",
        limit,
    )
    return [ClaimedEvent(**dict(r)) for r in rows]


async def mark_event_done(db: Database, event: ClaimedEvent, *, note: str | None = None) -> None:
    await db.execute(
        "UPDATE agent_events SET status = 'done', processed_at = now(), last_error = $3 "
        "WHERE tenant_id = $1 AND event_id = $2",
        event.tenant_id, event.event_id, note,
    )


def backoff(attempts: int) -> timedelta:
    """Exponential backoff (30 s, 60 s, 120 s, ...) with +/-20 % jitter."""
    base = 30 * 2 ** max(0, attempts - 1)
    return timedelta(seconds=base * random.uniform(0.8, 1.2))  # noqa: S311 -- jitter, not crypto


async def mark_event_failed_attempt(
    db: Database, event: ClaimedEvent, *, error: str, permanent: bool = False,
) -> str:
    """Retry later (``pending`` + backoff) or dead-letter (``failed``). Returns the new status."""
    status = "failed" if permanent or event.attempts >= MAX_EVENT_ATTEMPTS else "pending"
    await db.execute(
        "UPDATE agent_events SET status = $3, last_error = $4, "
        "available_at = now() + make_interval(secs => $5) "
        "WHERE tenant_id = $1 AND event_id = $2",
        event.tenant_id, event.event_id, status, error[:500],
        backoff(event.attempts).total_seconds(),
    )
    return status


async def reap_stale_events_system(db: Database, *, lease_seconds: int) -> int:
    """``processing`` past the lease (worker died) -> ``pending`` again."""
    rows = await db.fetch(
        "UPDATE agent_events SET status = 'pending', last_error = 'LEASE_EXPIRED' "
        "WHERE status = 'processing' AND processed_at < now() - make_interval(secs => $1) "
        "RETURNING event_id",
        lease_seconds,
    )
    return len(rows)
