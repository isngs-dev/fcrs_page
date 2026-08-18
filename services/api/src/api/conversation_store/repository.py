"""Conversation store repository -- tenant-scoped, idempotent append.

Every method takes ``AuthClaims`` and filters by ``claims.tenant_id`` at the
repository layer.  ``PLATFORM_ADMIN`` (global) is rejected with a
``ValidationError``.  ``VISITOR`` callers are additionally scoped to their own
``visitor_id``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import NotFoundError, ValidationError

from api.config import get_api_settings
from api.llm.provider import ChatMessage

if TYPE_CHECKING:
    from api.llm.provider import LLMProvider


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    status: str
    channel: str
    visitor_id: str | None
    started_at: datetime
    ended_at: datetime | None
    metadata: dict[str, Any]
    summary: str | None
    summary_message_count: int


@dataclass(frozen=True)
class Message:
    message_id: str
    role: str
    content: str
    intent: str | None
    confidence: float | None
    tokens: int | None
    created_at: datetime
    sources: list[dict[str, Any]] | None = None
    decision: str | None = None
    grounded: bool | None = None
    guardrail_flag: str | None = None
    action: str | None = None
    source_count: int = 0
    """Cheap ``jsonb_array_length(sources)`` projection (SR-2) -- only
    ``get_messages`` selects this; other constructors (``get_message``,
    ``get_window``) leave it at the default 0 (they already return the full
    ``sources`` payload where needed, so this hint isn't required there)."""


@dataclass(frozen=True)
class ConversationSummaryRow:
    """A single row in the ``GET /admin/conversations`` list (S12.4)."""

    conversation_id: str
    status: str
    channel: str
    visitor_id: str | None
    started_at: datetime
    ended_at: datetime | None
    message_count: int
    summary: str | None


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN)."""
    if claims.tenant_id is None:
        raise ValidationError("Conversation store is tenant-scoped.")


def _scope_filter(claims: AuthClaims) -> tuple[str, list[Any]]:
    """Extra WHERE clause + params for VISITOR scoping against ``conversations``
    (which has its own ``visitor_id`` column) -- visitor param is $3 in the
    queries that use this, which bind tenant_id=$1, conversation_id=$2 first.
    CLIENT_ADMIN / CLIENT_AGENT: empty clause.

    Do NOT use this against ``messages`` -- that table has no ``visitor_id``
    column of its own (migration 0007); VISITOR scoping there must go through
    an ``EXISTS (SELECT 1 FROM conversations ...)`` join instead (see
    ``get_message``, ``get_messages``, ``get_window``)."""
    if claims.role == Role.VISITOR:
        return " AND visitor_id = $3", [claims.subject]
    return "", []


async def _verify_conversation_visible(
    db: Database, claims: AuthClaims, conversation_id: str,
) -> None:
    """Raise ``NotFoundError`` if the conversation is not visible to the caller."""
    extra, extra_params = _scope_filter(claims)
    # Parameterized SQL; `extra` is a safe constant clause from _scope_filter.
    # ruff: noqa: S608
    sql = (
        "SELECT 1 FROM conversations "
        "WHERE tenant_id = $1 AND conversation_id = $2" + extra
    )
    row = await db.fetchrow(sql, claims.tenant_id, conversation_id, *extra_params)
    if row is None:
        raise NotFoundError(
            "Conversation not found.",
            code="CONVERSATION_NOT_FOUND",
        )


async def create_conversation(
    db: Database,
    claims: AuthClaims,
    *,
    visitor_id: str | None = None,
    channel: str = "widget",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create a new conversation for the caller's tenant.

    Returns the generated ``conversation_id`` (32-char hex).
    For ``VISITOR`` callers, ``visitor_id`` defaults to ``claims.subject``.
    """
    _reject_global(claims)

    if claims.role == Role.VISITOR and visitor_id is None:
        visitor_id = claims.subject

    conversation_id = uuid4().hex
    meta = metadata or {}

    await db.execute(
        "INSERT INTO conversations "
        "(conversation_id, tenant_id, visitor_id, status, channel, metadata) "
        "VALUES ($1, $2, $3, 'active', $4, $5)",
        conversation_id,
        claims.tenant_id,
        visitor_id,
        channel,
        meta,
    )
    return conversation_id


async def append_message(
    db: Database,
    claims: AuthClaims,
    conversation_id: str,
    *,
    role: str,
    content: str,
    intent: str | None = None,
    confidence: float | None = None,
    tokens: int | None = None,
    message_id: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    decision: str | None = None,
    grounded: bool | None = None,
    guardrail_flag: str | None = None,
    action: str | None = None,
) -> str:
    """Append a message to a conversation (idempotent by ``message_id``).

    Raises ``NotFoundError`` if the conversation is not visible to the caller.
    Returns the ``message_id`` (supplied or generated). ``sources`` (S10.1) is
    the list of cited chunks for an assistant turn. ``decision``/``grounded``
    (S10.2) tag the 3-way decision + whether the reply was grounded in
    retrieved context. ``guardrail_flag`` (S10.3) tags the violated output-
    guardrail rule name on a blocked turn, ``NULL`` on a clean one. ``action``
    (S10.4) tags the CTA signal (``"schedule_cta"``/``"lead_form"``/``NULL``)
    as a stored fact, so idempotent replay returns it verbatim. Existing
    callers that omit any of these bind ``NULL``, unchanged.
    """
    _reject_global(claims)
    await _verify_conversation_visible(db, claims, conversation_id)

    message_id = message_id or uuid4().hex

    await db.execute(
        "INSERT INTO messages "
        "(message_id, tenant_id, conversation_id, role, content, "
        " intent, confidence, tokens, sources, decision, grounded, "
        " guardrail_flag, action) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
        "ON CONFLICT (tenant_id, conversation_id, message_id) DO NOTHING",
        message_id,
        claims.tenant_id,
        conversation_id,
        role,
        content,
        intent,
        confidence,
        tokens,
        sources,
        decision,
        grounded,
        guardrail_flag,
        action,
    )
    return message_id


async def get_message(
    db: Database,
    claims: AuthClaims,
    conversation_id: str,
    message_id: str,
) -> Message | None:
    """Fetch a single message by id if visible to the caller.

    Mirrors ``_verify_conversation_visible``: a message is only visible if
    its conversation is visible to the caller. VISITOR callers are
    additionally scoped to their own ``visitor_id`` via a join on
    ``conversations`` (messages carry no ``visitor_id`` column of their own).
    Returns ``None`` if absent or not visible. Parameterized, positional.
    """
    _reject_global(claims)

    params: list[Any] = [claims.tenant_id, conversation_id, message_id]
    extra = ""
    if claims.role == Role.VISITOR:
        params.append(claims.subject)
        extra = (
            " AND EXISTS (SELECT 1 FROM conversations c "
            "WHERE c.conversation_id = messages.conversation_id "
            "AND c.tenant_id = messages.tenant_id "
            f"AND c.visitor_id = ${len(params)})"
        )
    # Parameterized SQL; `extra` is a safe constant clause built above.
    # ruff: noqa: S608
    sql = (
        "SELECT message_id, role, content, intent, confidence, tokens, "
        "created_at, sources, decision, grounded, guardrail_flag, action "
        "FROM messages "
        "WHERE tenant_id = $1 AND conversation_id = $2 AND message_id = $3" + extra
    )
    row = await db.fetchrow(sql, *params)
    if row is None:
        return None

    return Message(
        message_id=str(row["message_id"]),
        role=str(row["role"]),
        content=str(row["content"]),
        intent=row["intent"],
        confidence=row["confidence"],
        tokens=row["tokens"],
        created_at=row["created_at"],
        sources=row["sources"],
        decision=row.get("decision"),
        grounded=row.get("grounded"),
        guardrail_flag=row.get("guardrail_flag"),
        action=row.get("action"),
    )


async def count_messages(
    db: Database,
    claims: AuthClaims,
    conversation_id: str,
    *,
    role: str | None = None,
    decision: str | None = None,
) -> int:
    """Count messages in a conversation, tenant- (+ VISITOR-) scoped (S10.4).

    Used by the orchestrator's turn-cap check --
    ``count_messages(db, claims, conversation_id, role="user")`` counts this
    conversation's visitor turns. ``decision`` (SR-14 D10, additive) narrows
    to a specific stored ``decision`` tag -- used as
    ``count_messages(db, claims, conversation_id, role="bot",
    decision="identity_gate")`` to count identity-gate replies, so the
    orchestrator can net them out of the turn-cap's visitor-turn count
    without a new ``messages`` column. ``messages`` carries no ``visitor_id``
    column of its own (migration 0007); VISITOR callers are scoped via the
    same ``EXISTS (SELECT 1 FROM conversations ...)`` join
    ``get_message``/``get_messages``/``get_window`` use -- NOT the older,
    broken ``_scope_filter`` bare-column pattern (that raises
    ``asyncpg.UndefinedColumnError`` against ``messages``). Parameterized,
    positional placeholders, built in order (never a hardcoded index).
    Raises ``ValidationError`` for global (PLATFORM_ADMIN) callers.
    """
    _reject_global(claims)

    params: list[Any] = [claims.tenant_id, conversation_id]
    where = "WHERE tenant_id = $1 AND conversation_id = $2"

    if role is not None:
        params.append(role)
        where += f" AND role = ${len(params)}"

    if decision is not None:
        params.append(decision)
        where += f" AND decision = ${len(params)}"

    if claims.role == Role.VISITOR:
        params.append(claims.subject)
        where += (
            " AND EXISTS (SELECT 1 FROM conversations c "
            "WHERE c.conversation_id = messages.conversation_id "
            "AND c.tenant_id = messages.tenant_id "
            f"AND c.visitor_id = ${len(params)})"
        )

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    sql = "SELECT count(*) AS count FROM messages " + where
    row = await db.fetchrow(sql, *params)
    return int(row["count"]) if row is not None else 0


async def get_last_assistant_decision(
    db: Database,
    claims: AuthClaims,
    conversation_id: str,
) -> str | None:
    """Return the most recent assistant turn's decision, or ``None``.

    This narrow SR-13 read is tenant- and VISITOR-scoped exactly like
    ``count_messages``. ``messages`` has no ``visitor_id`` column, so visitor
    scoping uses the owning ``conversations`` row. Same-timestamp turns are
    ordered deterministically by ``message_id`` after ``created_at``.
    """
    _reject_global(claims)

    params: list[Any] = [claims.tenant_id, conversation_id, "bot"]
    where = "WHERE tenant_id = $1 AND conversation_id = $2"
    where += f" AND role = ${len(params)}"

    if claims.role == Role.VISITOR:
        params.append(claims.subject)
        where += (
            " AND EXISTS (SELECT 1 FROM conversations c "
            "WHERE c.conversation_id = messages.conversation_id "
            "AND c.tenant_id = messages.tenant_id "
            f"AND c.visitor_id = ${len(params)})"
        )

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    sql = (
        "SELECT decision FROM messages "
        + where
        + " ORDER BY created_at DESC, message_id DESC LIMIT 1"
    )
    row = await db.fetchrow(sql, *params)
    if row is None or row["decision"] is None:
        return None
    return str(row["decision"])


async def get_conversation(
    db: Database, claims: AuthClaims, conversation_id: str,
) -> Conversation | None:
    """Fetch a conversation if visible to the caller.

    Returns ``None`` if absent or not visible.
    """
    _reject_global(claims)

    extra, extra_params = _scope_filter(claims)
    # Parameterized SQL; `extra` is a safe constant clause from _scope_filter.
    # ruff: noqa: S608
    sql = (
        "SELECT conversation_id, status, channel, visitor_id, "
        "started_at, ended_at, metadata, summary, summary_message_count "
        "FROM conversations "
        "WHERE tenant_id = $1 AND conversation_id = $2" + extra
    )
    row = await db.fetchrow(sql, claims.tenant_id, conversation_id, *extra_params)
    if row is None:
        return None

    return Conversation(
        conversation_id=str(row["conversation_id"]),
        status=str(row["status"]),
        channel=str(row["channel"]),
        visitor_id=row["visitor_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        metadata=row["metadata"] or {},
        summary=row["summary"],
        summary_message_count=int(row["summary_message_count"]),
    )


async def get_messages(
    db: Database, claims: AuthClaims, conversation_id: str,
) -> list[Message]:
    """Fetch all messages for a visible conversation, ordered by creation.

    Raises ``NotFoundError`` if the conversation is not visible to the caller.
    """
    _reject_global(claims)
    await _verify_conversation_visible(db, claims, conversation_id)

    # `messages` carries no `visitor_id` column of its own (migration 0007) --
    # VISITOR callers must be scoped via an EXISTS join to `conversations`,
    # mirroring `get_message`'s pattern. Build positionally — NEVER a
    # hardcoded index.
    params: list[Any] = [claims.tenant_id, conversation_id]
    extra = ""
    if claims.role == Role.VISITOR:
        params.append(claims.subject)
        extra = (
            " AND EXISTS (SELECT 1 FROM conversations c "
            "WHERE c.conversation_id = messages.conversation_id "
            "AND c.tenant_id = messages.tenant_id "
            f"AND c.visitor_id = ${len(params)})"
        )
    # Parameterized SQL; `extra` is a safe constant clause built above.
    # ruff: noqa: S608
    sql = (
        "SELECT message_id, role, content, intent, confidence, tokens, "
        "created_at, coalesce(jsonb_array_length(sources), 0) AS source_count "
        "FROM messages "
        "WHERE tenant_id = $1 AND conversation_id = $2" + extra + " "
        "ORDER BY created_at, message_id"
    )
    rows = await db.fetch(sql, *params)
    return [
        Message(
            message_id=str(r["message_id"]),
            role=str(r["role"]),
            content=str(r["content"]),
            intent=r["intent"],
            confidence=r["confidence"],
            tokens=r["tokens"],
            created_at=r["created_at"],
            source_count=int(r["source_count"]) if "source_count" in r else 0,
        )
        for r in rows
    ]


async def list_conversations(
    db: Database,
    claims: AuthClaims,
    *,
    limit: int = 50,
    offset: int = 0,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    status: str | None = None,
    channel: str | None = None,
    escalated: bool | None = None,
    visitor_ids: list[str] | None = None,
) -> tuple[list[ConversationSummaryRow], int]:
    """Fetch a paginated, filtered page of the caller's tenant conversations.

    Tenant-scoped (``WHERE c.tenant_id = $1``); each supplied filter appends
    exactly one positional clause, values always bound (never interpolated).
    ``escalated`` toggles a fixed constant ``EXISTS``/``NOT EXISTS``
    sub-select on ``messages`` (bot turn with ``decision='escalate'``) in
    Python -- the sub-select text itself carries no bound values.
    VISITOR scoping is not relevant here -- this is an admin/agent-only
    surface reached via ``require_roles(CLIENT_ADMIN, CLIENT_AGENT)``, which
    excludes VISITOR; ``_reject_global`` excludes PLATFORM_ADMIN.

    ``visitor_ids`` (SR-9.3 J6/Scope §4): optional -- filters to conversations
    whose ``visitor_id`` is in the given list (``c.visitor_id = ANY($n)``).
    Used by the timeline fan-out to resolve "which conversations belong to
    this identity set" without any SQL against another module's tables.
    Existing callers are unaffected by the ``None`` default.

    Returns ``(rows, total)`` -- ``total`` is a ``count(*)`` over the same
    filtered WHERE (minus LIMIT/OFFSET), newest first
    (``ORDER BY c.started_at DESC, c.conversation_id DESC``).
    """
    _reject_global(claims)

    where = "WHERE c.tenant_id = $1"
    params: list[Any] = [claims.tenant_id]

    if started_from is not None:
        params.append(started_from)
        where += f" AND c.started_at >= ${len(params)}"
    if started_to is not None:
        params.append(started_to)
        where += f" AND c.started_at < ${len(params)}"
    if status is not None:
        params.append(status)
        where += f" AND c.status = ${len(params)}"
    if channel is not None:
        params.append(channel)
        where += f" AND c.channel = ${len(params)}"
    if visitor_ids is not None:
        params.append(visitor_ids)
        where += f" AND c.visitor_id = ANY(${len(params)})"

    _escalate_sub = (
        "SELECT 1 FROM messages m WHERE m.tenant_id = c.tenant_id "
        "AND m.conversation_id = c.conversation_id "
        "AND m.role = 'bot' AND m.decision = 'escalate'"
    )
    if escalated is True:
        where += f" AND EXISTS ({_escalate_sub})"
    elif escalated is False:
        where += f" AND NOT EXISTS ({_escalate_sub})"

    # Parameterized SQL; `where` is a safe constant clause built above --
    # filter values are always bound, never interpolated.
    # ruff: noqa: S608
    count_row = await db.fetchrow(
        "SELECT count(*) AS count FROM conversations c " + where, *params,
    )
    total = int(count_row["count"]) if count_row is not None else 0

    clamped_limit = max(1, min(limit, 200))
    page_params = [*params, clamped_limit, max(0, offset)]
    rows = await db.fetch(
        "SELECT c.conversation_id, c.status, c.channel, c.visitor_id, "
        "c.started_at, c.ended_at, c.summary, "
        "(SELECT count(*) FROM messages m WHERE m.tenant_id = c.tenant_id "
        "AND m.conversation_id = c.conversation_id) AS message_count "
        "FROM conversations c " + where + " "
        f"ORDER BY c.started_at DESC, c.conversation_id DESC "
        f"LIMIT ${len(page_params) - 1} OFFSET ${len(page_params)}",
        *page_params,
    )
    return [
        ConversationSummaryRow(
            conversation_id=str(r["conversation_id"]),
            status=str(r["status"]),
            channel=str(r["channel"]),
            visitor_id=r["visitor_id"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            message_count=int(r["message_count"]),
            summary=r["summary"],
        )
        for r in rows
    ], total


def _estimate_tokens(content: str) -> int:
    """Estimate token count from content length (~4 chars/token).

    Returns at least 1 so empty content still counts.
    """
    return max(1, len(content) // 4)


async def get_window(
    db: Database,
    claims: AuthClaims,
    conversation_id: str,
    *,
    limit: int | None = None,
    token_budget: int | None = None,
) -> list[Message]:
    """Fetch the most recent messages within a count or token budget.

    Exactly one of ``limit`` or ``token_budget`` must be provided and ≥1.
    Returns messages in chronological order (oldest→newest).

    Token accounting uses the stored ``tokens`` column when present, else
    estimates via ``max(1, len(content) // 4)``.
    """
    # Validate exactly-one-mode + positivity
    if (limit is None and token_budget is None) or (limit is not None and token_budget is not None):
        raise ValidationError(
            "Provide exactly one of limit or token_budget.",
            code="INVALID_WINDOW_ARGS",
        )
    if limit is not None and limit < 1:
        raise ValidationError(
            "limit must be ≥ 1.",
            code="INVALID_WINDOW_ARGS",
        )
    if token_budget is not None and token_budget < 1:
        raise ValidationError(
            "token_budget must be ≥ 1.",
            code="INVALID_WINDOW_ARGS",
        )

    _reject_global(claims)
    await _verify_conversation_visible(db, claims, conversation_id)

    # Build SQL positionally — NEVER a hardcoded index.
    # `messages` carries no `visitor_id` column of its own (migration 0007) --
    # VISITOR callers must be scoped via an EXISTS join to `conversations`,
    # mirroring `get_message`'s pattern (not a bare column on `messages`).
    params: list[Any] = [claims.tenant_id, conversation_id]
    where = "WHERE tenant_id = $1 AND conversation_id = $2"
    if claims.role == Role.VISITOR:
        params.append(claims.subject)
        where += (
            " AND EXISTS (SELECT 1 FROM conversations c "
            "WHERE c.conversation_id = messages.conversation_id "
            "AND c.tenant_id = messages.tenant_id "
            f"AND c.visitor_id = ${len(params)})"
        )

    base = (
        "SELECT message_id, role, content, intent, confidence, tokens, "
        "created_at "
        "FROM messages " + where + " "
        "ORDER BY created_at DESC, message_id DESC"
    )

    if limit is not None:
        params.append(limit)
        sql = base + f" LIMIT ${len(params)}"
        rows = await db.fetch(sql, *params)
        msgs = [
            Message(
                message_id=str(r["message_id"]),
                role=str(r["role"]),
                content=str(r["content"]),
                intent=r["intent"],
                confidence=r["confidence"],
                tokens=r["tokens"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
        msgs.reverse()
        return msgs

    # token_budget mode: fetch with a safety cap, then trim in Python.
    assert token_budget is not None  # noqa: S101 — validated above; type narrowing
    _SAFETY_CAP = 500
    params.append(_SAFETY_CAP)
    sql = base + f" LIMIT ${len(params)}"
    rows = await db.fetch(sql, *params)

    msgs = [
        Message(
            message_id=str(r["message_id"]),
            role=str(r["role"]),
            content=str(r["content"]),
            intent=r["intent"],
            confidence=r["confidence"],
            tokens=r["tokens"],
            created_at=r["created_at"],
        )
        for r in rows
    ]

    # Walk newest→oldest (already in that order), accumulate tokens.
    kept: list[Message] = []
    running = 0
    for m in msgs:
        tok = m.tokens if m.tokens is not None else _estimate_tokens(m.content)
        if running + tok <= token_budget:
            running += tok
            kept.append(m)
        elif not kept:
            # ≥1 rule: always include the most recent message.
            kept.append(m)
            break
        else:
            break

    kept.reverse()
    return kept


_SUMMARY_SYSTEM_PROMPT = (
    "You maintain a concise running summary of an ongoing chat for context. "
    "Combine the existing summary and the new messages into an updated summary "
    "that preserves key facts, the visitor's questions/intent, and any "
    "commitments. Reply with only the summary."
)


def _build_summary_prompt(
    existing_summary: str | None, folded: list[Message],
) -> list[ChatMessage]:
    """Build the [system, user] prompt for a summary roll (decision 5)."""
    lines = [f"{m.role}: {m.content}" for m in folded]
    user_parts = [
        f"Existing summary:\n{existing_summary or '(none yet)'}",
        "New messages to fold in:\n" + "\n".join(lines),
    ]
    return [
        ChatMessage("system", _SUMMARY_SYSTEM_PROMPT),
        ChatMessage("user", "\n\n".join(user_parts)),
    ]


async def roll_summary(
    db: Database,
    claims: AuthClaims,
    conversation_id: str,
    *,
    provider: LLMProvider,
    model: str,
    keep_recent: int,
) -> bool:
    """Fold older messages into the running summary via the injected provider.

    Folds ``messages[summary_message_count : new_count]`` where
    ``new_count = max(0, total - keep_recent)``. If that slice is empty, this
    is a no-op (returns ``False``, provider not called, no UPDATE).

    No silent fallback: if ``provider.generate`` raises, the exception
    propagates and neither the summary nor the watermark is updated.

    Raises ``ValidationError`` (``INVALID_SUMMARY_ARGS``) if ``keep_recent`` <
    0, ``ValidationError`` for global callers, and ``NotFoundError`` if the
    conversation is not visible to the caller.
    """
    if keep_recent < 0:
        raise ValidationError(
            "keep_recent must be >= 0.",
            code="INVALID_SUMMARY_ARGS",
        )

    _reject_global(claims)

    conv = await get_conversation(db, claims, conversation_id)
    if conv is None:
        raise NotFoundError(
            "Conversation not found.",
            code="CONVERSATION_NOT_FOUND",
        )

    messages = await get_messages(db, claims, conversation_id)
    total = len(messages)
    new_count = max(0, total - keep_recent)

    folded = messages[conv.summary_message_count : new_count]
    if not folded:
        return False

    prompt = _build_summary_prompt(conv.summary, folded)
    settings = get_api_settings()
    completion = await provider.generate(
        prompt, model=model, max_tokens=settings.llm_max_tokens,
    )
    new_summary = completion.text

    extra, extra_params = _scope_filter(claims)
    # Parameterized SQL; `extra` is a safe constant clause from _scope_filter.
    # Build positionally — NEVER a hardcoded index.
    params: list[Any] = [new_summary, new_count, claims.tenant_id, conversation_id]
    sql = (
        "UPDATE conversations SET summary = $1, summary_message_count = $2 "
        "WHERE tenant_id = $3 AND conversation_id = $4"
    )
    if extra:
        # Re-number the VISITOR clause's placeholder to follow the SET params.
        sql += f" AND visitor_id = ${len(params) + 1}"
        params.extend(extra_params)

    await db.execute(sql, *params)
    return True


async def get_working_memory(
    db: Database,
    claims: AuthClaims,
    conversation_id: str,
    *,
    keep_recent: int,
) -> dict[str, Any]:
    """Return ``{summary, summary_message_count, messages}`` for the conversation.

    ``messages`` is the last ``keep_recent`` chronological messages (reuses
    ``get_window``). The summary covers older turns; the window covers the
    recent tail. Raises ``ValidationError`` (``INVALID_SUMMARY_ARGS``) if
    ``keep_recent`` < 1, ``ValidationError`` for global callers, and
    ``NotFoundError`` if the conversation is not visible to the caller.
    """
    if keep_recent < 1:
        raise ValidationError(
            "keep_recent must be >= 1.",
            code="INVALID_SUMMARY_ARGS",
        )

    _reject_global(claims)

    conv = await get_conversation(db, claims, conversation_id)
    if conv is None:
        raise NotFoundError(
            "Conversation not found.",
            code="CONVERSATION_NOT_FOUND",
        )

    msgs = await get_window(db, claims, conversation_id, limit=keep_recent)

    return {
        "summary": conv.summary,
        "summary_message_count": conv.summary_message_count,
        "messages": msgs,
    }


async def export_conversation(
    db: Database, claims: AuthClaims, conversation_id: str,
) -> dict[str, Any]:
    """Export a full tenant-scoped transcript (data portability).

    Returns the conversation + its messages with ``tenant_id`` stripped.
    Raises ``NotFoundError`` if not visible.
    """
    _reject_global(claims)

    conv = await get_conversation(db, claims, conversation_id)
    if conv is None:
        raise NotFoundError(
            "Conversation not found.",
            code="CONVERSATION_NOT_FOUND",
        )

    msgs = await get_messages(db, claims, conversation_id)

    return {
        "conversation_id": conv.conversation_id,
        "status": conv.status,
        "channel": conv.channel,
        "started_at": conv.started_at,
        "ended_at": conv.ended_at,
        "summary": conv.summary,
        "summary_message_count": conv.summary_message_count,
        "messages": [
            {
                "message_id": m.message_id,
                "role": m.role,
                "content": m.content,
                "intent": m.intent,
                "confidence": m.confidence,
                "tokens": m.tokens,
                "created_at": m.created_at,
            }
            for m in msgs
        ],
    }


async def delete_conversation(
    db: Database, claims: AuthClaims, conversation_id: str,
) -> bool:
    """Delete a conversation (right-to-erasure; messages cascade).

    Verifies visibility BEFORE issuing the DELETE.
    Raises ``NotFoundError`` if not visible.
    Returns ``True`` on success.
    """
    _reject_global(claims)
    await _verify_conversation_visible(db, claims, conversation_id)

    # Build positionally — NEVER a hardcoded index.
    params: list[Any] = [claims.tenant_id, conversation_id]
    sql = "DELETE FROM conversations WHERE tenant_id = $1 AND conversation_id = $2"
    if claims.role == Role.VISITOR:
        params.append(claims.subject)
        sql += f" AND visitor_id = ${len(params)}"

    await db.execute(sql, *params)
    return True


async def purge_expired(
    db: Database, claims: AuthClaims, *, before: datetime,
) -> int:
    """Purge ended conversations older than ``before``.

    Returns the number of conversations deleted.
    """
    _reject_global(claims)

    status = await db.execute(
        "DELETE FROM conversations "
        "WHERE tenant_id = $1 AND ended_at IS NOT NULL AND ended_at < $2",
        claims.tenant_id,
        before,
    )
    # asyncpg returns "DELETE <n>" as the status string.
    parts = status.split()
    if len(parts) == 2 and parts[0].upper() == "DELETE":
        return int(parts[1])
    return 0


@dataclass(frozen=True)
class ClosedConversation:
    """A single row flipped active -> ended by ``close_idle_conversations``."""

    conversation_id: str
    tenant_id: str


async def close_idle_conversations(db: Database, *, idle_minutes: int) -> list[ClosedConversation]:
    """Beat-driven idle-timeout sweep (SR-25): flip every ``active``
    conversation whose most recent message -- or, for a conversation with no
    messages yet, its ``started_at`` -- is older than ``idle_minutes`` to
    ``status='ended'`` + ``ended_at=now()``.

    System-scoped -- no ``claims`` argument, mirroring
    ``scheduling.claim_due_reminders``: Celery Beat has no tenant context,
    and a single tick sweeps every tenant at once. The one
    ``UPDATE ... WHERE status = 'active' ... RETURNING`` statement IS the
    exactly-once gate; unlike ``claim_due_reminders`` there is no separate
    claim-then-dispatch step to guard against double-processing (this
    function is the whole unit of work), so no ``FOR UPDATE SKIP LOCKED`` +
    ``LIMIT`` batching is needed -- Postgres re-evaluates ``WHERE status =
    'active'`` after acquiring each row's lock, so a second concurrent run
    (there should never be one; Beat is a singleton) would simply see
    already-``ended`` rows and update nothing.

    Before this existed, NOTHING in the codebase ever transitioned a
    conversation out of ``active`` -- the admin console's "Ended" tab was
    permanently empty and "Active" never shrank, no matter how old a
    conversation was.
    """
    rows = await db.fetch(
        "UPDATE conversations SET status = 'ended', ended_at = now() "
        "WHERE status = 'active' "
        "AND COALESCE("
        "  (SELECT MAX(m.created_at) FROM messages m "
        "   WHERE m.tenant_id = conversations.tenant_id "
        "   AND m.conversation_id = conversations.conversation_id), "
        "  started_at"
        ") <= now() - make_interval(mins => $1) "
        "RETURNING conversation_id, tenant_id",
        idle_minutes,
    )
    return [
        ClosedConversation(conversation_id=str(r["conversation_id"]), tenant_id=str(r["tenant_id"]))
        for r in rows
    ]
