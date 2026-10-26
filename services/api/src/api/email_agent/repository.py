"""Email threads + messages (migration 0067). Tenant-scoped; one message per task."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database

from api.agents.repository import _reject_global


@dataclass(frozen=True)
class EmailMessageRow:
    message_id: str
    thread_id: str
    contact_id: str | None  # None on an unmatched inbound thread (A3)
    plan_id: str | None
    direction: str
    rfc_message_id: str
    in_reply_to: str | None
    references: list[str]
    provider: str | None
    provider_message_id: str | None
    from_addr: str
    to_addr: str
    subject: str
    body_text: str
    status: str
    band: str | None
    sources: list[Any]
    task_id: str | None
    last_error: str | None
    approved_by: str | None
    created_at: datetime
    sent_at: datetime | None
    classification: str | None
    handled_action: str | None


_SELECT = (
    "SELECT m.message_id, m.thread_id, t.contact_id, t.plan_id, m.direction, m.rfc_message_id, "
    "m.in_reply_to, coalesce(m.\"references\", '{}') AS references, m.provider, "
    "m.provider_message_id, m.from_addr, m.to_addr, m.subject, m.body_text, m.status, m.band, "
    "m.sources, m.task_id, m.last_error, m.approved_by, m.created_at, m.sent_at, "
    "m.classification, m.handled_action "
    "FROM email_messages m JOIN email_threads t "
    "ON t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id "
)


def _row(row: Any) -> EmailMessageRow:
    values = dict(row)
    values["references"] = list(values["references"])
    return EmailMessageRow(**values)


async def get_or_create_thread(
    db: Database, claims: AuthClaims, *, contact_id: str, plan_id: str, subject: str,
) -> tuple[str, str]:
    """The plan's thread ``(thread_id, subject)`` -- created with ``subject`` if new."""
    _reject_global(claims)
    row = await db.fetchrow(
        "INSERT INTO email_threads (tenant_id, thread_id, contact_id, plan_id, subject) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (tenant_id, plan_id) WHERE plan_id IS NOT NULL "
        "DO UPDATE SET plan_id = EXCLUDED.plan_id RETURNING thread_id, subject",
        claims.tenant_id, uuid4().hex, contact_id, plan_id, subject,
    )
    assert row is not None  # noqa: S101 -- INSERT ... RETURNING always returns the row
    return str(row["thread_id"]), str(row["subject"])


async def thread_message_ids(db: Database, claims: AuthClaims, thread_id: str) -> list[str]:
    """RFC Message-IDs of the thread's sent/received messages, oldest first."""
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT rfc_message_id FROM email_messages WHERE tenant_id = $1 AND thread_id = $2 "
        "AND status IN ('sent', 'received') ORDER BY coalesce(sent_at, created_at)",
        claims.tenant_id, thread_id,
    )
    return [str(r["rfc_message_id"]) for r in rows]


async def insert_message(
    db: Database,
    claims: AuthClaims,
    *,
    thread_id: str,
    rfc_message_id: str,
    in_reply_to: str | None,
    references: list[str],
    from_addr: str,
    to_addr: str,
    subject: str,
    body_text: str,
    status: str,
    band: str,
    sources: list[Any],
    task_id: str,
) -> str:
    _reject_global(claims)
    message_id = uuid4().hex
    await db.execute(
        "INSERT INTO email_messages (tenant_id, message_id, thread_id, direction, rfc_message_id, "
        "in_reply_to, \"references\", from_addr, to_addr, subject, body_text, status, band, "
        "sources, task_id) "
        "VALUES ($1, $2, $3, 'outbound', $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
        claims.tenant_id, message_id, thread_id, rfc_message_id, in_reply_to, references or None,
        from_addr, to_addr, subject, body_text, status, band, sources, task_id,
    )
    return message_id


async def get_message(db: Database, claims: AuthClaims, message_id: str) -> EmailMessageRow | None:
    _reject_global(claims)
    row = await db.fetchrow(_SELECT + "WHERE m.tenant_id = $1 AND m.message_id = $2",
                            claims.tenant_id, message_id)
    return _row(row) if row is not None else None


async def get_message_for_task(
    db: Database, claims: AuthClaims, task_id: str,
) -> EmailMessageRow | None:
    _reject_global(claims)
    row = await db.fetchrow(_SELECT + "WHERE m.tenant_id = $1 AND m.task_id = $2",
                            claims.tenant_id, task_id)
    return _row(row) if row is not None else None


async def list_messages(
    db: Database, claims: AuthClaims, *, status: str | None, limit: int, offset: int,
    direction: str | None = None, contact_id: str | None = None,
) -> list[EmailMessageRow]:
    _reject_global(claims)
    rows = await db.fetch(
        _SELECT + "WHERE m.tenant_id = $1 AND ($2::text IS NULL OR m.status = $2) "
        "AND ($5::text IS NULL OR m.direction = $5) AND ($6::text IS NULL OR t.contact_id = $6) "
        "ORDER BY m.created_at DESC LIMIT $3 OFFSET $4",
        claims.tenant_id, status, max(1, min(limit, 200)), max(0, offset), direction, contact_id,
    )
    return [_row(r) for r in rows]


async def counts_since(db: Database, claims: AuthClaims, since: datetime) -> dict[str, int]:
    """Drafts waiting for review now, and emails sent since ``since`` (A8 overview)."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT count(*) FILTER (WHERE status = 'draft') AS drafts, "
        "count(*) FILTER (WHERE status = 'sent' AND sent_at >= $2) AS sent "
        "FROM email_messages WHERE tenant_id = $1",
        claims.tenant_id, since)
    assert row is not None  # noqa: S101 -- an aggregate always returns a row
    return {"drafts": int(row["drafts"]), "sent": int(row["sent"])}


async def set_status(
    db: Database,
    claims: AuthClaims,
    message_id: str,
    *,
    status: str,
    from_statuses: tuple[str, ...],
    error: str | None = None,
    approved_by: str | None = None,
) -> bool:
    """Compare-and-set status. ``False`` when the message wasn't in ``from_statuses``."""
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE email_messages SET status = $3, last_error = coalesce($5, last_error), "
        "approved_by = coalesce($6, approved_by) "
        "WHERE tenant_id = $1 AND message_id = $2 AND status = ANY($4::text[])",
        claims.tenant_id, message_id, status, list(from_statuses), error, approved_by,
    )
    return str(tag).endswith(" 1")


async def mark_sent(
    db: Database, claims: AuthClaims, message_id: str, *, provider: str,
    provider_message_id: str | None,
) -> None:
    _reject_global(claims)
    await db.execute(
        "UPDATE email_messages SET status = 'sent', sent_at = now(), provider = $3, "
        "provider_message_id = $4, last_error = NULL WHERE tenant_id = $1 AND message_id = $2",
        claims.tenant_id, message_id, provider, provider_message_id,
    )
    await db.execute(
        "UPDATE email_threads t SET last_message_at = now() FROM email_messages m "
        "WHERE m.tenant_id = $1 AND m.message_id = $2 "
        "AND t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id",
        claims.tenant_id, message_id,
    )


async def update_draft(
    db: Database, claims: AuthClaims, message_id: str, *, subject: str, body_text: str,
) -> bool:
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE email_messages SET subject = $3, body_text = $4 "
        "WHERE tenant_id = $1 AND message_id = $2 AND status = 'draft'",
        claims.tenant_id, message_id, subject, body_text,
    )
    return str(tag).endswith(" 1")


@dataclass(frozen=True)
class ExpiredDraft:
    tenant_id: str
    message_id: str
    task_id: str | None


async def expire_drafts_system(db: Database, *, ttl_days: int) -> list[ExpiredDraft]:
    """System-scoped: drafts older than ``ttl_days`` -> ``discarded`` (``DRAFT_EXPIRED``)."""
    rows = await db.fetch(
        "UPDATE email_messages SET status = 'discarded', last_error = 'DRAFT_EXPIRED' "
        "WHERE status = 'draft' AND created_at < now() - make_interval(days => $1) "
        "RETURNING tenant_id, message_id, task_id",
        ttl_days,
    )
    return [ExpiredDraft(**dict(r)) for r in rows]


# -- Inbound (A3) ---------------------------------------------------------------------


async def thread_for_message_ids(
    db: Database, claims: AuthClaims, rfc_ids: list[str],
) -> str | None:
    """The thread one of our messages with any of these RFC ids belongs to (tenant-scoped)."""
    _reject_global(claims)
    if not rfc_ids:
        return None
    value = await db.fetchval(
        "SELECT thread_id FROM email_messages WHERE tenant_id = $1 "
        "AND rfc_message_id = ANY($2::text[]) ORDER BY created_at DESC LIMIT 1",
        claims.tenant_id, rfc_ids,
    )
    return str(value) if value is not None else None


async def thread_exists(db: Database, claims: AuthClaims, thread_id: str) -> bool:
    _reject_global(claims)
    return bool(await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM email_threads WHERE tenant_id = $1 AND thread_id = $2)",
        claims.tenant_id, thread_id))


async def newest_thread_for_contact(
    db: Database, claims: AuthClaims, contact_id: str,
) -> str | None:
    _reject_global(claims)
    value = await db.fetchval(
        "SELECT thread_id FROM email_threads WHERE tenant_id = $1 AND contact_id = $2 "
        "AND status = 'open' ORDER BY created_at DESC LIMIT 1",
        claims.tenant_id, contact_id,
    )
    return str(value) if value is not None else None


async def create_thread(
    db: Database, claims: AuthClaims, *, contact_id: str | None, subject: str, status: str,
) -> str:
    _reject_global(claims)
    thread_id = uuid4().hex
    await db.execute(
        "INSERT INTO email_threads (tenant_id, thread_id, contact_id, subject, status) "
        "VALUES ($1, $2, $3, $4, $5)",
        claims.tenant_id, thread_id, contact_id, subject or "(no subject)", status,
    )
    return thread_id


async def insert_inbound(
    db: Database,
    claims: AuthClaims,
    *,
    thread_id: str,
    rfc_message_id: str,
    in_reply_to: str | None,
    references: list[str],
    from_addr: str,
    to_addr: str,
    subject: str,
    body_text: str,
    reply_text: str,
    auth: dict[str, Any],
) -> str | None:
    """Store a received message. ``None`` when it was already stored (provider retry)."""
    _reject_global(claims)
    message_id = uuid4().hex
    value = await db.fetchval(
        "INSERT INTO email_messages (tenant_id, message_id, thread_id, direction, rfc_message_id, "
        "in_reply_to, \"references\", from_addr, to_addr, subject, body_text, reply_text, status, "
        "auth) VALUES ($1, $2, $3, 'inbound', $4, $5, $6, $7, $8, $9, $10, $11, 'received', $12) "
        "ON CONFLICT (tenant_id, rfc_message_id) DO NOTHING RETURNING message_id",
        claims.tenant_id, message_id, thread_id, rfc_message_id, in_reply_to, references or None,
        from_addr, to_addr, subject, body_text, reply_text, auth,
    )
    if value is not None:
        await db.execute(
            "UPDATE email_threads SET last_message_at = now() WHERE tenant_id = $1 "
            "AND thread_id = $2", claims.tenant_id, thread_id)
    return str(value) if value is not None else None


async def get_inbound_context(
    db: Database, claims: AuthClaims, message_id: str,
) -> dict[str, Any] | None:
    """What the reply handler needs about a received message (tenant-scoped)."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT m.message_id, m.thread_id, t.contact_id, t.plan_id, t.subject AS thread_subject, "
        "m.rfc_message_id, coalesce(m.\"references\", '{}') AS references, m.from_addr, "
        "m.subject, m.reply_text, m.auth, m.classification, m.handled_action "
        "FROM email_messages m JOIN email_threads t "
        "ON t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id "
        "WHERE m.tenant_id = $1 AND m.message_id = $2 AND m.direction = 'inbound'",
        claims.tenant_id, message_id,
    )
    if row is None:
        return None
    values = dict(row)
    values["references"] = list(values["references"])
    return values


async def record_handling(
    db: Database, claims: AuthClaims, message_id: str, *, classification: str, source: str,
    action: str,
) -> None:
    _reject_global(claims)
    await db.execute(
        "UPDATE email_messages SET classification = $3, classification_source = $4, "
        "handled_action = $5 WHERE tenant_id = $1 AND message_id = $2",
        claims.tenant_id, message_id, classification, source, action,
    )


async def insert_reply(
    db: Database,
    claims: AuthClaims,
    *,
    thread_id: str,
    rfc_message_id: str,
    in_reply_to: str,
    references: list[str],
    from_addr: str,
    to_addr: str,
    subject: str,
    body_text: str,
    status: str,
    band: str,
    sources: list[Any],
) -> str:
    """An outbound answer in an existing thread (no outreach task)."""
    _reject_global(claims)
    message_id = uuid4().hex
    await db.execute(
        "INSERT INTO email_messages (tenant_id, message_id, thread_id, direction, rfc_message_id, "
        "in_reply_to, \"references\", from_addr, to_addr, subject, body_text, status, band, "
        "sources) VALUES ($1, $2, $3, 'outbound', $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
        claims.tenant_id, message_id, thread_id, rfc_message_id, in_reply_to, references,
        from_addr, to_addr, subject, body_text, status, band, sources,
    )
    return message_id


async def delete_for_contact(db: Database, claims: AuthClaims, contact_id: str) -> int:
    """Delete the contact's threads; their messages cascade (A9 H9). Joins the caller's
    transaction. Returns threads deleted."""
    _reject_global(claims)
    rows = await db.fetch("DELETE FROM email_threads WHERE tenant_id = $1 AND contact_id = $2 "
                          "RETURNING thread_id", claims.tenant_id, contact_id)
    return len(rows)
