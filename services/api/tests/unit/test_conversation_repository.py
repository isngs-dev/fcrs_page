"""Unit tests for the conversation store repository.

Covers:
- create_conversation inserts with the caller's tenant_id; returns 32-char hex id.
- Tenant isolation: get_conversation/get_messages/append_message bind WHERE tenant_id.
- VISITOR scoping: VISITOR binds extra visitor_id filter; CLIENT_ADMIN does not.
- Idempotent append: INSERT contains ON CONFLICT (message_id) DO NOTHING.
- Global caller (PLATFORM_ADMIN) → ValidationError on every method.
- Missing conversation → NotFoundError (CONVERSATION_NOT_FOUND).
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError

from api.conversation_store.repository import (
    append_message,
    count_messages,
    create_conversation,
    delete_conversation,
    export_conversation,
    get_conversation,
    get_last_assistant_decision,
    get_message,
    get_recent_assistant_decisions,
    get_messages,
    get_window,
    get_working_memory,
    purge_expired,
    roll_summary,
)
from api.llm.provider import ChatMessage, Completion, LLMError


class _RecordingDatabase:
    """Database double that records SQL + params and returns canned rows."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.last_sql: str = ""
        self.last_params: tuple[Any, ...] = ()
        self.all_sql: list[str] = []
        self.all_params: list[tuple[Any, ...]] = []
        self._rows = rows or []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.last_sql = query
        self.last_params = args
        self.all_sql.append(query)
        self.all_params.append(args)
        return self._rows[0] if self._rows else None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.last_sql = query
        self.last_params = args
        self.all_sql.append(query)
        self.all_params.append(args)
        return self._rows

    async def execute(self, query: str, *args: Any) -> str:
        self.last_sql = query
        self.last_params = args
        self.all_sql.append(query)
        self.all_params.append(args)
        return "INSERT 1"

    async def close(self) -> None:
        pass


def _claims(tenant_id: str | None, role: Role, subject: str = "user-1") -> AuthClaims:
    return AuthClaims(subject=subject, role=role, tenant_id=tenant_id)


# -- create_conversation -------------------------------------------------------


async def test_create_conversation_inserts_with_callers_tenant_id() -> None:
    """create_conversation INSERT carries claims.tenant_id, not from any argument."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    conv_id = await create_conversation(db, claims, channel="widget")

    assert re.fullmatch(r"[0-9a-f]{32}", conv_id), f"Expected 32-char hex, got {conv_id}"
    assert "tenant_id" in db.last_sql
    # conversation_id is $1, tenant_id is $2
    assert db.last_params[1] == "tenant-a"


async def test_create_conversation_sets_visitor_id_from_subject_for_visitor() -> None:
    """VISITOR caller: visitor_id defaults to claims.subject."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    await create_conversation(db, claims)

    # visitor_id should be bound in the INSERT params
    assert "visitor-xyz" in db.last_params


async def test_create_conversation_returns_active_status() -> None:
    """create_conversation INSERT includes status='active'."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await create_conversation(db, claims)

    assert "active" in db.last_sql


# -- get_conversation ----------------------------------------------------------


async def test_get_conversation_filters_by_tenant_id() -> None:
    """get_conversation SELECT binds WHERE tenant_id = caller's tenant."""
    row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    result = await get_conversation(db, claims, "conv-1")

    assert result is not None
    assert result.conversation_id == "conv-1"
    assert "tenant_id" in db.last_sql
    assert db.last_params[0] == "tenant-a"


async def test_get_conversation_visitor_scopes_by_visitor_id() -> None:
    """VISITOR caller: SELECT additionally binds visitor_id = $3 as the 3rd param."""
    row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": "visitor-xyz",
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    result = await get_conversation(db, claims, "conv-1")

    assert result is not None
    assert "visitor_id = $3" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "visitor-xyz")


async def test_get_conversation_client_admin_does_not_scope_visitor_id() -> None:
    """CLIENT_ADMIN caller: SELECT does NOT bind visitor_id filter."""
    row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": "visitor-xyz",
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    result = await get_conversation(db, claims, "conv-1")

    assert result is not None
    # visitor_id may appear in SELECT columns but not in WHERE clause
    assert "visitor_id =" not in db.last_sql


async def test_get_conversation_not_found_returns_none() -> None:
    """Missing conversation → None."""
    db = _RecordingDatabase(rows=[])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    result = await get_conversation(db, claims, "nonexistent")

    assert result is None


# -- get_messages --------------------------------------------------------------


async def test_get_messages_filters_by_tenant_and_conversation() -> None:
    """get_messages SELECT binds tenant_id + conversation_id."""
    now = datetime.now(UTC)
    rows = [
        {
            "message_id": "msg-1",
            "role": "user",
            "content": "Hello",
            "intent": None,
            "confidence": None,
            "tokens": None,
            "created_at": now,
        },
    ]
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    messages = await get_messages(db, claims, "conv-1")

    assert len(messages) == 1
    assert messages[0].message_id == "msg-1"
    assert messages[0].role == "user"
    assert messages[0].content == "Hello"
    assert "tenant_id" in db.last_sql
    assert "conversation_id" in db.last_sql
    assert db.last_params[0] == "tenant-a"
    assert db.last_params[1] == "conv-1"


async def test_get_messages_visitor_scopes_by_visitor_id() -> None:
    """get_messages: VISITOR caller binds visitor_id = $3 as the 3rd param."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": "visitor-xyz",
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    # We need the visibility check to pass first, then get_messages does fetch
    # For simplicity, test the visibility check SQL directly
    from api.conversation_store.repository import _verify_conversation_visible

    await _verify_conversation_visible(db, claims, "conv-1")

    assert "visitor_id = $3" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "visitor-xyz")


async def test_get_messages_visitor_query_scopes_via_conversations_join() -> None:
    """Regression guard: messages has NO visitor_id column (migration 0007).
    get_messages's own SELECT (not just the visibility check) must scope a
    VISITOR caller via an EXISTS join to conversations, never a bare
    `visitor_id = $N` clause directly against `messages` (that raises
    asyncpg.UndefinedColumnError in production)."""
    now = datetime.now(UTC)
    msg_row = {
        "message_id": "msg-1",
        "role": "user",
        "content": "Hello",
        "intent": None,
        "confidence": None,
        "tokens": None,
        "created_at": now,
    }
    db = _SequencedDatabase(
        fetchrow_results=[_visible_row()],
        fetch_results=[[msg_row]],
    )
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    messages = await get_messages(db, claims, "conv-1")

    assert len(messages) == 1
    # The messages SELECT is the second SQL call (after the visibility fetchrow).
    messages_sql = db.all_sql[1]
    assert "EXISTS (SELECT 1 FROM conversations" in messages_sql
    assert "c.visitor_id" in messages_sql
    # Negative guard: no bare/unqualified visitor_id column reference on messages.
    assert " AND visitor_id = " not in messages_sql
    assert "visitor-xyz" in db.all_params[1]


async def test_get_messages_not_visible_raises_not_found() -> None:
    """get_messages: if conversation not visible to caller → NotFoundError."""
    db = _RecordingDatabase(rows=[])  # No conversation row
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(NotFoundError) as exc_info:
        await get_messages(db, claims, "conv-other")

    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"


# -- append_message ------------------------------------------------------------


async def test_append_message_inserts_with_tenant_id() -> None:
    """append_message INSERT carries claims.tenant_id."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg_id = await append_message(db, claims, "conv-1", role="user", content="Hello")

    assert re.fullmatch(r"[0-9a-f]{32}", msg_id), f"Expected 32-char hex, got {msg_id}"
    assert "tenant_id" in db.last_sql
    # message_id is $1, tenant_id is $2
    assert db.last_params[1] == "tenant-a"


async def test_append_message_idempotent_with_supplied_id() -> None:
    """append_message with message_id: INSERT contains the composite ON CONFLICT
    target (tenant_id, conversation_id, message_id) DO NOTHING, scoping
    idempotency to the conversation (not globally across tenants/conversations)."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg_id = await append_message(
        db, claims, "conv-1", role="user", content="Hello", message_id="m-1",
    )

    assert msg_id == "m-1"
    assert "ON CONFLICT (tenant_id, conversation_id, message_id) DO NOTHING" in db.last_sql


async def test_append_message_not_visible_raises_not_found() -> None:
    """append_message: if conversation not visible to caller → NotFoundError."""
    db = _RecordingDatabase(rows=[])  # No conversation row
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(NotFoundError) as exc_info:
        await append_message(db, claims, "conv-other", role="user", content="Hello")

    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"


async def test_append_message_visitor_scopes_by_visitor_id() -> None:
    """append_message: VISITOR caller's visibility check binds visitor_id = $3."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": "visitor-xyz",
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    await append_message(db, claims, "conv-1", role="user", content="Hello")

    # The visibility check (fetchrow) is the first SQL call
    assert "visitor_id = $3" in db.all_sql[0]
    assert db.all_params[0] == ("tenant-a", "conv-1", "visitor-xyz")


# -- PLATFORM_ADMIN rejected ---------------------------------------------------


async def test_platform_admin_rejected_on_create() -> None:
    """PLATFORM_ADMIN (tenant_id=None) → ValidationError on create_conversation."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await create_conversation(db, claims)


async def test_platform_admin_rejected_on_get() -> None:
    """PLATFORM_ADMIN → ValidationError on get_conversation."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_conversation(db, claims, "conv-1")


async def test_platform_admin_rejected_on_get_messages() -> None:
    """PLATFORM_ADMIN → ValidationError on get_messages."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_messages(db, claims, "conv-1")


async def test_platform_admin_rejected_on_append() -> None:
    """PLATFORM_ADMIN → ValidationError on append_message."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await append_message(db, claims, "conv-1", role="user", content="Hello")


# -- get_window ----------------------------------------------------------------


def _msg_row(message_id: str, role: str, content: str, tokens: int | None, ts: datetime) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "role": role,
        "content": content,
        "intent": None,
        "confidence": None,
        "tokens": tokens,
        "created_at": ts,
    }


async def test_get_window_limit_returns_last_n_chronological() -> None:
    """get_window(limit=3) returns 3 messages oldest→newest."""
    now = datetime.now(UTC)
    rows = [
        _msg_row("m5", "user", "five", None, now),
        _msg_row("m4", "bot", "four", None, now),
        _msg_row("m3", "user", "three", None, now),
    ]
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msgs = await get_window(db, claims, "conv-1", limit=3)

    assert len(msgs) == 3
    # Reversed from newest-first → oldest→newest
    assert msgs[0].content == "three"
    assert msgs[1].content == "four"
    assert msgs[2].content == "five"
    # Assert ORDER BY ... DESC ... LIMIT $3
    assert "ORDER BY created_at DESC, message_id DESC" in db.last_sql
    assert "LIMIT $3" in db.last_sql


async def test_get_window_visitor_param_numbering() -> None:
    """VISITOR get_window(limit=2): visitor_id = $3 AND LIMIT $4."""
    now = datetime.now(UTC)
    rows = [
        _msg_row("m2", "bot", "two", None, now),
        _msg_row("m1", "user", "one", None, now),
    ]
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    msgs = await get_window(db, claims, "conv-1", limit=2)

    assert len(msgs) == 2
    assert "visitor_id = $3" in db.last_sql
    assert "LIMIT $4" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "visitor-xyz", 2)


async def test_get_window_limit_visitor_query_scopes_via_conversations_join() -> None:
    """Regression guard: messages has NO visitor_id column (migration 0007).
    get_window's limit-mode SELECT must scope a VISITOR caller via an EXISTS
    join to conversations, never a bare `visitor_id = $N` clause directly
    against `messages`."""
    now = datetime.now(UTC)
    rows = [_msg_row("m2", "bot", "two", None, now), _msg_row("m1", "user", "one", None, now)]
    db = _SequencedDatabase(
        fetchrow_results=[_visible_row()],
        fetch_results=[rows],
    )
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    msgs = await get_window(db, claims, "conv-1", limit=2)

    assert len(msgs) == 2
    window_sql = db.all_sql[1]
    assert "EXISTS (SELECT 1 FROM conversations" in window_sql
    assert "c.visitor_id" in window_sql
    assert " AND visitor_id = " not in window_sql
    assert "LIMIT $4" in window_sql
    assert db.all_params[1] == ("tenant-a", "conv-1", "visitor-xyz", 2)


async def test_get_window_token_budget_visitor_query_scopes_via_conversations_join() -> None:
    """Same regression guard as above, for get_window's token_budget branch
    (which shares the buggy `where`/`base` string construction)."""
    now = datetime.now(UTC)
    rows = [_msg_row("m2", "bot", "two", 5, now), _msg_row("m1", "user", "one", 3, now)]
    db = _SequencedDatabase(
        fetchrow_results=[_visible_row()],
        fetch_results=[rows],
    )
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    msgs = await get_window(db, claims, "conv-1", token_budget=10)

    assert len(msgs) == 2
    window_sql = db.all_sql[1]
    assert "EXISTS (SELECT 1 FROM conversations" in window_sql
    assert "c.visitor_id" in window_sql
    assert " AND visitor_id = " not in window_sql
    assert "visitor-xyz" in db.all_params[1]


async def test_get_window_token_budget_with_stored_tokens() -> None:
    """token_budget includes messages that fit, newest-first, ≥1 always."""
    now = datetime.now(UTC)
    # newest first: m5(20tok), m4(15tok), m3(10tok), m2(5tok), m1(3tok)
    rows = [
        _msg_row("m5", "user", "five", 20, now),
        _msg_row("m4", "bot", "four", 15, now),
        _msg_row("m3", "user", "three", 10, now),
        _msg_row("m2", "bot", "two", 5, now),
        _msg_row("m1", "user", "one", 3, now),
    ]
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    # Budget 18: m5(20) exceeds alone → return just m5 (≥1 rule)
    msgs = await get_window(db, claims, "conv-1", token_budget=18)
    assert len(msgs) == 1
    assert msgs[0].message_id == "m5"

    # Budget 30: m5(20)+m4(15)=35 > 30, so just m5
    msgs = await get_window(db, claims, "conv-1", token_budget=30)
    assert len(msgs) == 1
    assert msgs[0].message_id == "m5"

    # Budget 35: m5(20)+m4(15)=35 fits → return [m4, m5] chronological
    msgs = await get_window(db, claims, "conv-1", token_budget=35)
    assert len(msgs) == 2
    assert msgs[0].message_id == "m4"
    assert msgs[1].message_id == "m5"


async def test_get_window_token_budget_null_tokens_estimate() -> None:
    """token_budget with tokens=None uses len(content)//4 estimate."""
    now = datetime.now(UTC)
    # "hello world" = 11 chars → 11//4 = 2 tokens
    # "short" = 5 chars → 5//4 = 1 token
    rows = [
        _msg_row("m2", "user", "hello world", None, now),  # est 2
        _msg_row("m1", "bot", "short", None, now),  # est 1
    ]
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    # Budget 1: m2(est 2) exceeds → return just m2 (≥1 rule)
    msgs = await get_window(db, claims, "conv-1", token_budget=1)
    assert len(msgs) == 1
    assert msgs[0].message_id == "m2"

    # Budget 3: m2(2)+m1(1)=3 fits → return [m1, m2] chronological
    msgs = await get_window(db, claims, "conv-1", token_budget=3)
    assert len(msgs) == 2
    assert msgs[0].message_id == "m1"
    assert msgs[1].message_id == "m2"


async def test_get_window_exactly_one_mode_validation() -> None:
    """Neither or both → ValidationError INVALID_WINDOW_ARGS."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_window(db, claims, "conv-1")
    assert exc_info.value.code == "INVALID_WINDOW_ARGS"

    with pytest.raises(ValidationError) as exc_info:
        await get_window(db, claims, "conv-1", limit=2, token_budget=10)
    assert exc_info.value.code == "INVALID_WINDOW_ARGS"


async def test_get_window_invalid_limit_or_budget() -> None:
    """limit=0 or negative → ValidationError."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_window(db, claims, "conv-1", limit=0)
    assert exc_info.value.code == "INVALID_WINDOW_ARGS"

    with pytest.raises(ValidationError) as exc_info:
        await get_window(db, claims, "conv-1", limit=-1)
    assert exc_info.value.code == "INVALID_WINDOW_ARGS"

    with pytest.raises(ValidationError) as exc_info:
        await get_window(db, claims, "conv-1", token_budget=0)
    assert exc_info.value.code == "INVALID_WINDOW_ARGS"


async def test_get_window_not_visible_raises_not_found() -> None:
    """get_window: if conversation not visible → NotFoundError."""
    db = _RecordingDatabase(rows=[])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(NotFoundError) as exc_info:
        await get_window(db, claims, "conv-other", limit=5)
    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"


async def test_get_window_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on get_window."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_window(db, claims, "conv-1", limit=5)


# -- roll_summary / get_working_memory ------------------------------------------


class _StubProvider:
    """Stub LLMProvider.generate -- records the messages it was called with."""

    def __init__(
        self, *, text: str = "Summary text.", error: LLMError | None = None,
    ) -> None:
        self._text = text
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int,
    ) -> Completion:
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        if self._error is not None:
            raise self._error
        return Completion(text=self._text, model=model, input_tokens=1, output_tokens=1)


class _SequencedDatabase:
    """Database double that serves canned responses for a sequence of calls.

    ``fetchrow_results`` is consumed in order by successive ``fetchrow`` calls
    (visibility check, then conversation row, etc). ``fetch_results`` likewise
    for ``fetch`` calls (e.g. all messages, then the window query).
    ``execute_status`` can be overridden to return a custom status string.
    """

    def __init__(
        self,
        *,
        fetchrow_results: list[dict[str, Any] | None] | None = None,
        fetch_results: list[list[dict[str, Any]]] | None = None,
        execute_status: str = "UPDATE 1",
    ) -> None:
        self._fetchrow_results = list(fetchrow_results or [])
        self._fetch_results = list(fetch_results or [])
        self._execute_status = execute_status
        self.all_sql: list[str] = []
        self.all_params: list[tuple[Any, ...]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.all_sql.append(query)
        self.all_params.append(args)
        if not self._fetchrow_results:
            return None
        return self._fetchrow_results.pop(0)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.all_sql.append(query)
        self.all_params.append(args)
        if not self._fetch_results:
            return []
        return self._fetch_results.pop(0)

    async def execute(self, query: str, *args: Any) -> str:
        self.all_sql.append(query)
        self.all_params.append(args)
        self.execute_calls.append((query, args))
        return self._execute_status

    async def close(self) -> None:
        pass


def _visible_row() -> dict[str, Any]:
    return {"conversation_id": "conv-1"}


def _conv_row(
    *, summary: str | None = None, summary_message_count: int = 0,
) -> dict[str, Any]:
    return {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": summary,
        "summary_message_count": summary_message_count,
    }


def _all_msg_rows(n: int) -> list[dict[str, Any]]:
    """n chronological messages m1..mn, oldest first."""
    now = datetime.now(UTC)
    return [
        _msg_row(f"m{i}", "user" if i % 2 else "bot", f"content-{i}", None, now)
        for i in range(1, n + 1)
    ]


async def test_roll_summary_folds_messages_into_prompt_and_updates() -> None:
    """roll_summary folds messages[summary_message_count : total-keep_recent],
    calls the provider, and UPDATEs with the returned summary + new watermark.
    The folded messages' content appears in the prompt passed to the provider."""
    db = _SequencedDatabase(
        fetchrow_results=[_conv_row(summary=None, summary_message_count=0), _visible_row()],
        fetch_results=[_all_msg_rows(6)],
    )
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    provider = _StubProvider(text="Rolled summary.")

    rolled = await roll_summary(
        db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=2,
    )

    assert rolled is True
    assert len(provider.calls) == 1
    prompt_messages = provider.calls[0]["messages"]
    full_prompt = " ".join(m.content for m in prompt_messages)
    # messages[0:4] = m1..m4 folded (m5, m6 are the kept recent tail)
    for i in range(1, 5):
        assert f"content-{i}" in full_prompt
    for i in range(5, 7):
        assert f"content-{i}" not in full_prompt

    assert len(db.execute_calls) == 1
    update_sql, update_params = db.execute_calls[0]
    assert "UPDATE conversations" in update_sql
    assert "Rolled summary." in update_params
    assert 4 in update_params
    # Guard: conversations table has no updated_at column — must never appear in SET.
    assert "updated_at" not in update_sql


async def test_roll_summary_no_op_when_nothing_new_to_fold() -> None:
    """total - keep_recent <= summary_message_count → no-op, provider not called,
    no UPDATE issued."""
    db = _SequencedDatabase(
        fetchrow_results=[_conv_row(summary="Old summary.", summary_message_count=4), _visible_row()],
        fetch_results=[_all_msg_rows(6)],
    )
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    provider = _StubProvider()

    rolled = await roll_summary(
        db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=2,
    )

    assert rolled is False
    assert provider.calls == []
    assert db.execute_calls == []


async def test_roll_summary_llm_error_propagates_no_update() -> None:
    """Provider raises LLMError → roll_summary propagates it; no UPDATE issued;
    watermark/summary untouched (no silent fallback)."""
    db = _SequencedDatabase(
        fetchrow_results=[_conv_row(summary=None, summary_message_count=0), _visible_row()],
        fetch_results=[_all_msg_rows(6)],
    )
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    provider = _StubProvider(error=LLMError("upstream failed"))

    with pytest.raises(LLMError):
        await roll_summary(
            db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=2,
        )

    assert len(provider.calls) == 1
    assert db.execute_calls == []


async def test_roll_summary_invalid_keep_recent_raises_validation_error() -> None:
    """keep_recent < 0 → ValidationError INVALID_SUMMARY_ARGS."""
    db = _SequencedDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    provider = _StubProvider()

    with pytest.raises(ValidationError) as exc_info:
        await roll_summary(
            db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=-1,
        )
    assert exc_info.value.code == "INVALID_SUMMARY_ARGS"
    assert provider.calls == []


async def test_roll_summary_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on roll_summary."""
    db = _SequencedDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)
    provider = _StubProvider()

    with pytest.raises(ValidationError):
        await roll_summary(
            db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=2,
        )


async def test_roll_summary_not_visible_raises_not_found() -> None:
    """Conversation not visible → NotFoundError; provider not called."""
    db = _SequencedDatabase(fetchrow_results=[None])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    provider = _StubProvider()

    with pytest.raises(NotFoundError) as exc_info:
        await roll_summary(
            db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=2,
        )
    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"
    assert provider.calls == []


async def test_roll_summary_update_does_not_reference_updated_at() -> None:
    """roll_summary UPDATE must NOT reference updated_at — the conversations table
    has no such column (only started_at / ended_at). Regression guard for the
    asyncpg.UndefinedColumnError that was triggered by POST /debug/conversations/{id}/summary.
    Also asserts that summary=$1, new_count=$2, tenant_id=$3, conversation_id=$4 are bound."""
    db = _SequencedDatabase(
        fetchrow_results=[_conv_row(summary=None, summary_message_count=0), _visible_row()],
        fetch_results=[_all_msg_rows(6)],
    )
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    provider = _StubProvider(text="Guard summary.")

    rolled = await roll_summary(
        db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=2,
    )

    assert rolled is True
    update_sql, update_params = db.execute_calls[0]
    # Negative guard: must never reference the non-existent column.
    assert "updated_at" not in update_sql
    # Positive guard: key fields are bound in positional order.
    assert update_params[0] == "Guard summary."  # summary = $1
    assert update_params[1] == 4                  # summary_message_count = $2 (6 total - 2 kept)
    assert update_params[2] == "tenant-a"         # tenant_id = $3
    assert update_params[3] == "conv-1"           # conversation_id = $4


async def test_roll_summary_visitor_update_placeholder_numbering() -> None:
    """VISITOR-scoped UPDATE: placeholders numbered by position, never hardcoded.
    Expected param order: summary, new_count, tenant_id, conversation_id, visitor_id."""
    db = _SequencedDatabase(
        fetchrow_results=[_conv_row(summary=None, summary_message_count=0), _visible_row()],
        fetch_results=[_all_msg_rows(6)],
    )
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")
    provider = _StubProvider(text="Rolled summary.")

    rolled = await roll_summary(
        db, claims, "conv-1", provider=provider, model="claude-opus-4-8", keep_recent=2,
    )

    assert rolled is True
    update_sql, update_params = db.execute_calls[0]
    assert "visitor_id = $5" in update_sql
    assert update_params == ("Rolled summary.", 4, "tenant-a", "conv-1", "visitor-xyz")


async def test_get_working_memory_returns_summary_and_messages() -> None:
    """get_working_memory returns {summary, summary_message_count, messages} where
    messages = get_window(limit=keep_recent) (last keep_recent, chronological)."""
    db = _SequencedDatabase(
        fetchrow_results=[
            _conv_row(summary="Existing summary.", summary_message_count=4),
            _visible_row(),
        ],
        fetch_results=[_all_msg_rows(2)[::-1]],  # newest-first as get_window's SELECT expects
    )
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    result = await get_working_memory(db, claims, "conv-1", keep_recent=2)

    assert result["summary"] == "Existing summary."
    assert result["summary_message_count"] == 4
    assert len(result["messages"]) == 2


async def test_get_working_memory_invalid_keep_recent_raises_validation_error() -> None:
    """keep_recent < 1 → ValidationError INVALID_SUMMARY_ARGS."""
    db = _SequencedDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_working_memory(db, claims, "conv-1", keep_recent=0)
    assert exc_info.value.code == "INVALID_SUMMARY_ARGS"


async def test_get_working_memory_not_visible_raises_not_found() -> None:
    """Conversation not visible → NotFoundError."""
    db = _SequencedDatabase(fetchrow_results=[None])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(NotFoundError):
        await get_working_memory(db, claims, "conv-1", keep_recent=2)


async def test_get_working_memory_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on get_working_memory."""
    db = _SequencedDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_working_memory(db, claims, "conv-1", keep_recent=2)


async def test_get_working_memory_visitor_scoped() -> None:
    """VISITOR caller: visibility check scopes by visitor_id."""
    db = _SequencedDatabase(
        fetchrow_results=[
            _conv_row(summary=None, summary_message_count=0),
            _visible_row(),
        ],
        fetch_results=[[]],
    )
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    result = await get_working_memory(db, claims, "conv-1", keep_recent=2)

    assert result["summary"] is None
    assert "visitor_id = $3" in db.all_sql[0]


# -- export_conversation -------------------------------------------------------


async def test_export_conversation_returns_transcript() -> None:
    """export_conversation returns the transcript with messages + summary, no tenant_id."""
    db = _SequencedDatabase(
        fetchrow_results=[
            _conv_row(summary="Summary.", summary_message_count=2),  # get_conversation
            _visible_row(),  # get_messages visibility check
        ],
        fetch_results=[
            [
                _msg_row("m1", "user", "Hello", 5, datetime.now(UTC)),
                _msg_row("m2", "bot", "Hi there", 3, datetime.now(UTC)),
            ],
        ],
    )
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    transcript = await export_conversation(db, claims, "conv-1")

    assert transcript["conversation_id"] == "conv-1"
    assert transcript["status"] == "active"
    assert transcript["summary"] == "Summary."
    assert len(transcript["messages"]) == 2
    assert "tenant_id" not in transcript


async def test_export_conversation_not_visible_raises_not_found() -> None:
    """export_conversation: not visible → NotFoundError."""
    db = _SequencedDatabase(fetchrow_results=[None])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(NotFoundError) as exc_info:
        await export_conversation(db, claims, "conv-other")
    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"


async def test_export_conversation_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on export_conversation."""
    db = _SequencedDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await export_conversation(db, claims, "conv-1")


# -- delete_conversation -------------------------------------------------------


async def test_delete_conversation_issues_tenant_scoped_delete() -> None:
    """delete_conversation: DELETE WHERE tenant_id=$1 AND conversation_id=$2."""
    db = _SequencedDatabase(
        fetchrow_results=[_visible_row()],
    )
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    result = await delete_conversation(db, claims, "conv-1")

    assert result is True
    # First call is visibility check (fetchrow), second is DELETE (execute)
    assert "DELETE FROM conversations" in db.all_sql[1]
    assert "tenant_id = $1" in db.all_sql[1]
    assert "conversation_id = $2" in db.all_sql[1]
    assert db.all_params[1] == ("tenant-a", "conv-1")


async def test_delete_conversation_visitor_scopes_by_visitor_id() -> None:
    """delete_conversation: VISITOR adds AND visitor_id = $3."""
    db = _SequencedDatabase(
        fetchrow_results=[_visible_row()],
    )
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    await delete_conversation(db, claims, "conv-1")

    assert "visitor_id = $3" in db.all_sql[1]
    assert db.all_params[1] == ("tenant-a", "conv-1", "visitor-xyz")


async def test_delete_conversation_not_visible_raises_not_found() -> None:
    """delete_conversation: not visible → NotFoundError."""
    db = _SequencedDatabase(fetchrow_results=[None])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    with pytest.raises(NotFoundError) as exc_info:
        await delete_conversation(db, claims, "conv-other")
    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"


async def test_delete_conversation_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on delete_conversation."""
    db = _SequencedDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await delete_conversation(db, claims, "conv-1")


# -- purge_expired -------------------------------------------------------------


async def test_purge_expired_issues_tenant_scoped_delete() -> None:
    """purge_expired: DELETE WHERE tenant_id=$1 AND ended_at IS NOT NULL AND ended_at < $2."""
    db = _SequencedDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    before = datetime(2025, 1, 1, tzinfo=UTC)

    await purge_expired(db, claims, before=before)

    # _SequencedDatabase.execute always returns "UPDATE 1", so count will be 0
    # but we can still verify the SQL and params are correct.
    assert "DELETE FROM conversations" in db.all_sql[0]
    assert "tenant_id = $1" in db.all_sql[0]
    assert "ended_at IS NOT NULL" in db.all_sql[0]
    assert "ended_at < $2" in db.all_sql[0]
    assert db.all_params[0] == ("tenant-a", before)


async def test_purge_expired_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on purge_expired."""
    db = _SequencedDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await purge_expired(db, claims, before=datetime.now(UTC))


async def test_purge_expired_parses_delete_count() -> None:
    """purge_expired parses the "DELETE 3" status string and returns 3."""
    db = _SequencedDatabase(execute_status="DELETE 3")
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    before = datetime(2025, 1, 1, tzinfo=UTC)

    count = await purge_expired(db, claims, before=before)

    assert count == 3


# -- get_message (S10.1) --------------------------------------------------------


def _message_row_with_sources(
    message_id: str = "msg-a",
    role: str = "bot",
    content: str = "The answer.",
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "role": role,
        "content": content,
        "intent": None,
        "confidence": 0.8,
        "tokens": 12,
        "created_at": datetime.now(UTC),
        "sources": sources,
    }


async def test_get_message_binds_tenant_conversation_message_positional() -> None:
    """get_message SELECT binds WHERE tenant_id=$1 AND conversation_id=$2 AND
    message_id=$3, positional placeholders."""
    row = _message_row_with_sources(sources=[{"doc_id": "d1", "chunk_id": "c1", "score": 0.9, "matched_by": ["vector"]}])
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert msg.message_id == "msg-a"
    assert msg.sources == [{"doc_id": "d1", "chunk_id": "c1", "score": 0.9, "matched_by": ["vector"]}]
    assert "tenant_id = $1" in db.last_sql
    assert "conversation_id = $2" in db.last_sql
    assert "message_id = $3" in db.last_sql
    assert db.last_params[0] == "tenant-a"
    assert db.last_params[1] == "conv-1"
    assert db.last_params[2] == "msg-a"


async def test_get_message_visitor_scoped_via_conversation_join() -> None:
    """VISITOR caller: get_message adds a visitor_id scope (mirrors
    _verify_conversation_visible), binding the visitor subject as an extra param."""
    row = _message_row_with_sources()
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert "visitor-xyz" in db.last_params
    assert "visitor_id" in db.last_sql


async def test_get_message_not_visible_returns_none() -> None:
    """Absent/not-visible message → None (not an exception)."""
    db = _RecordingDatabase(rows=[])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-missing")

    assert msg is None


async def test_get_message_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on get_message."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_message(db, claims, "conv-1", "msg-a")


# -- append_message sources (S10.1) ---------------------------------------------


async def test_append_message_binds_sources_param() -> None:
    """append_message(sources=[...]) binds the sources param in the INSERT."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    sources = [{"doc_id": "d1", "chunk_id": "c1", "score": 0.9, "matched_by": ["vector"]}]

    await append_message(
        db, claims, "conv-1", role="bot", content="Answer.", sources=sources,
    )

    assert "sources" in db.last_sql
    assert sources in db.last_params


async def test_append_message_without_sources_binds_null() -> None:
    """Existing callers (no sources kwarg) still bind NULL for sources."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await append_message(db, claims, "conv-1", role="user", content="Hello")

    assert None in db.last_params


# -- append_message decision/grounded (S10.2) ------------------------------------


async def test_append_message_binds_decision_and_grounded_params() -> None:
    """append_message(decision=..., grounded=...) binds them in the INSERT."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await append_message(
        db, claims, "conv-1", role="bot", content="Answer.",
        decision="clarify", grounded=False,
    )

    assert "decision" in db.last_sql
    assert "grounded" in db.last_sql
    assert "clarify" in db.last_params
    assert False in db.last_params


async def test_append_message_without_decision_grounded_binds_null() -> None:
    """Existing callers (no decision/grounded kwargs) still bind NULL."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await append_message(db, claims, "conv-1", role="user", content="Hello")

    # Last two positional params are decision, grounded -- both NULL.
    assert db.last_params[-2] is None
    assert db.last_params[-1] is None


# -- get_message decision/grounded round-trip (S10.2) -----------------------------


def _message_row_with_decision(
    message_id: str = "msg-a",
    role: str = "bot",
    content: str = "The answer.",
    decision: str | None = "clarify",
    grounded: bool | None = False,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "role": role,
        "content": content,
        "intent": "question",
        "confidence": 0.4,
        "tokens": None,
        "created_at": datetime.now(UTC),
        "sources": [],
        "decision": decision,
        "grounded": grounded,
    }


async def test_get_message_selects_and_returns_decision_and_grounded() -> None:
    row = _message_row_with_decision(decision="escalate", grounded=False)
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert msg.decision == "escalate"
    assert msg.grounded is False
    assert "decision" in db.last_sql
    assert "grounded" in db.last_sql


# -- append_message guardrail_flag (S10.3) ----------------------------------------


async def test_append_message_binds_guardrail_flag_param() -> None:
    """append_message(guardrail_flag=...) binds it in the INSERT."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await append_message(
        db, claims, "conv-1", role="bot", content="Safe reply.",
        decision="blocked", grounded=False, guardrail_flag="instruction_leak",
    )

    assert "guardrail_flag" in db.last_sql
    assert "instruction_leak" in db.last_params


async def test_append_message_without_guardrail_flag_binds_null() -> None:
    """Existing callers (no guardrail_flag kwarg) still bind NULL."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await append_message(db, claims, "conv-1", role="user", content="Hello")

    # Last positional param is guardrail_flag -- NULL.
    assert db.last_params[-1] is None


# -- get_message guardrail_flag round-trip (S10.3) ---------------------------------


def _message_row_with_guardrail_flag(
    message_id: str = "msg-a",
    role: str = "bot",
    content: str = "Safe reply.",
    decision: str | None = "blocked",
    grounded: bool | None = False,
    guardrail_flag: str | None = "instruction_leak",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "role": role,
        "content": content,
        "intent": "question",
        "confidence": 0.8,
        "tokens": None,
        "created_at": datetime.now(UTC),
        "sources": [],
        "decision": decision,
        "grounded": grounded,
        "guardrail_flag": guardrail_flag,
    }


async def test_get_message_selects_and_returns_guardrail_flag() -> None:
    row = _message_row_with_guardrail_flag(guardrail_flag="human_impersonation")
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert msg.guardrail_flag == "human_impersonation"
    assert "guardrail_flag" in db.last_sql


async def test_get_message_guardrail_flag_none_when_clean() -> None:
    row = _message_row_with_guardrail_flag(decision="answer", grounded=True, guardrail_flag=None)
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert msg.guardrail_flag is None


# -- count_messages (S10.4) ------------------------------------------------------


async def test_count_messages_binds_tenant_and_conversation_positionally() -> None:
    """count_messages SELECT binds WHERE tenant_id=$1 AND conversation_id=$2,
    positional placeholders, and returns the stubbed count."""
    db = _RecordingDatabase(rows=[{"count": 3}])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    count = await count_messages(db, claims, "conv-1")

    assert count == 3
    assert "tenant_id = $1" in db.last_sql
    assert "conversation_id = $2" in db.last_sql
    assert db.last_params[0] == "tenant-a"
    assert db.last_params[1] == "conv-1"


async def test_count_messages_role_filter_binds_role_param() -> None:
    """count_messages(role="user") binds an extra AND role=$N clause."""
    db = _RecordingDatabase(rows=[{"count": 7}])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    count = await count_messages(db, claims, "conv-1", role="user")

    assert count == 7
    assert "role = $3" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "user")


async def test_count_messages_visitor_scopes_via_conversations_join() -> None:
    """Regression guard: messages has NO visitor_id column of its own
    (migration 0007) -- VISITOR scoping in count_messages MUST go through the
    same EXISTS (SELECT 1 FROM conversations ...) join get_message uses, NOT
    the old broken _scope_filter (bare `visitor_id = $N` against messages,
    which raises asyncpg.UndefinedColumnError in production)."""
    db = _RecordingDatabase(rows=[{"count": 2}])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    count = await count_messages(db, claims, "conv-1", role="user")

    assert count == 2
    assert "EXISTS (SELECT 1 FROM conversations" in db.last_sql
    assert "c.visitor_id" in db.last_sql
    # Negative guard: no bare/unqualified visitor_id column reference on messages.
    assert " AND visitor_id = " not in db.last_sql
    assert "visitor-xyz" in db.last_params


async def test_count_messages_visitor_role_param_numbering() -> None:
    """VISITOR + role: role=$3, visitor_id=$4 (positional, built in order)."""
    db = _RecordingDatabase(rows=[{"count": 5}])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    await count_messages(db, claims, "conv-1", role="user")

    assert "role = $3" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "user", "visitor-xyz")


async def test_count_messages_global_caller_rejected() -> None:
    """PLATFORM_ADMIN → ValidationError on count_messages."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await count_messages(db, claims, "conv-1")


 # -- get_last_assistant_decision (SR-13) ------------------------------------------


async def test_get_last_assistant_decision_returns_latest_bot_decision_tenant_scoped() -> None:
    """The narrow SR-13 read is tenant/conversation scoped and reads bot turns only."""
    db = _RecordingDatabase(rows=[{"decision": "clarify"}])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    decision = await get_last_assistant_decision(db, claims, "conv-1")

    assert decision == "clarify"
    assert "WHERE tenant_id = $1 AND conversation_id = $2" in db.last_sql
    assert "role = $3" in db.last_sql
    assert "ORDER BY created_at DESC, message_id DESC" in db.last_sql
    assert "LIMIT 1" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "bot")


@pytest.mark.parametrize("rows", [[], [{"decision": None}]])
async def test_get_last_assistant_decision_returns_none_for_absent_or_null_decision(
    rows: list[dict[str, Any]],
) -> None:
    """No prior bot turn and a legacy NULL decision both mean no prior clarify."""
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    assert await get_last_assistant_decision(db, claims, "conv-1") is None


async def test_get_last_assistant_decision_visitor_scopes_through_conversation_owner() -> None:
    """A visitor cannot inspect another visitor's bot turn in the same tenant."""
    db = _RecordingDatabase(rows=[{"decision": "answer"}])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    decision = await get_last_assistant_decision(db, claims, "conv-1")

    assert decision == "answer"
    assert "EXISTS (SELECT 1 FROM conversations c" in db.last_sql
    assert "c.visitor_id = $4" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "bot", "visitor-xyz")


async def test_get_last_assistant_decision_rejects_global_caller_before_query() -> None:
    """PLATFORM_ADMIN has no tenant scope, so the read fails closed."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_last_assistant_decision(db, claims, "conv-1")

    assert db.all_sql == []


# -- get_recent_assistant_decisions (low-confidence streak check) ----------------


async def test_get_recent_assistant_decisions_returns_newest_first_tenant_scoped() -> None:
    db = _RecordingDatabase(rows=[{"decision": "escalate"}, {"decision": "clarify"}])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    decisions = await get_recent_assistant_decisions(db, claims, "conv-1", limit=3)

    assert decisions == ["escalate", "clarify"]
    assert "WHERE tenant_id = $1 AND conversation_id = $2" in db.last_sql
    assert "role = $3" in db.last_sql
    assert "ORDER BY created_at DESC, message_id DESC" in db.last_sql
    assert "LIMIT $4" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "bot", 3)


async def test_get_recent_assistant_decisions_maps_null_decision_to_none() -> None:
    """A legacy pre-0024 row with decision IS NULL maps to None, not a
    dropped/miscounted entry -- the caller (the streak check) must see it
    and treat it as a streak-breaker, not silently skip it."""
    db = _RecordingDatabase(rows=[{"decision": "clarify"}, {"decision": None}])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    decisions = await get_recent_assistant_decisions(db, claims, "conv-1", limit=5)

    assert decisions == ["clarify", None]


async def test_get_recent_assistant_decisions_returns_empty_list_for_no_rows() -> None:
    db = _RecordingDatabase(rows=[])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    assert await get_recent_assistant_decisions(db, claims, "conv-1", limit=3) == []


async def test_get_recent_assistant_decisions_visitor_scopes_through_conversation_owner() -> None:
    """A visitor cannot inspect another visitor's bot turns in the same tenant."""
    db = _RecordingDatabase(rows=[{"decision": "escalate"}])
    claims = _claims("tenant-a", Role.VISITOR, subject="visitor-xyz")

    decisions = await get_recent_assistant_decisions(db, claims, "conv-1", limit=3)

    assert decisions == ["escalate"]
    assert "EXISTS (SELECT 1 FROM conversations c" in db.last_sql
    assert "c.visitor_id = $4" in db.last_sql
    assert "LIMIT $5" in db.last_sql
    assert db.last_params == ("tenant-a", "conv-1", "bot", "visitor-xyz", 3)


async def test_get_recent_assistant_decisions_rejects_global_caller_before_query() -> None:
    """PLATFORM_ADMIN has no tenant scope, so the read fails closed."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_recent_assistant_decisions(db, claims, "conv-1", limit=3)

    assert db.all_sql == []


async def test_get_recent_assistant_decisions_tenant_isolation() -> None:
    """Tenant A's read must never be able to see tenant B's conversation --
    the WHERE clause binds tenant_id, not just conversation_id."""
    db = _RecordingDatabase(rows=[{"decision": "clarify"}])
    claims_a = _claims("tenant-a", Role.CLIENT_ADMIN)

    await get_recent_assistant_decisions(db, claims_a, "conv-shared", limit=3)

    assert db.last_params[0] == "tenant-a"
    assert "tenant_id = $1" in db.last_sql


# -- append_message / get_message action round-trip (S10.4) ----------------------


async def test_append_message_binds_action_param() -> None:
    """append_message(action=...) binds it in the INSERT."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await append_message(
        db, claims, "conv-1", role="bot", content="Book a call.",
        decision="escalate", action="schedule_cta",
    )

    assert "action" in db.last_sql
    assert "schedule_cta" in db.last_params


async def test_append_message_without_action_binds_null() -> None:
    """Existing callers (no action kwarg) still bind NULL for action."""
    conv_row = {
        "conversation_id": "conv-1",
        "status": "active",
        "channel": "widget",
        "visitor_id": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "metadata": {},
        "summary": None,
        "summary_message_count": 0,
    }
    db = _RecordingDatabase(rows=[conv_row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await append_message(db, claims, "conv-1", role="user", content="Hello")

    assert db.last_params[-1] is None


def _message_row_with_action(
    message_id: str = "msg-a",
    role: str = "bot",
    content: str = "Book a call.",
    decision: str | None = "escalate",
    action: str | None = "schedule_cta",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "role": role,
        "content": content,
        "intent": None,
        "confidence": None,
        "tokens": None,
        "created_at": datetime.now(UTC),
        "sources": [],
        "decision": decision,
        "grounded": False,
        "guardrail_flag": None,
        "action": action,
    }


async def test_get_message_selects_and_returns_action() -> None:
    row = _message_row_with_action(action="schedule_cta")
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert msg.action == "schedule_cta"
    assert "action" in db.last_sql


async def test_get_message_action_none_when_no_cta() -> None:
    row = _message_row_with_action(decision="answer", action=None)
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert msg.action is None


# ---------------------------------------------------------------------------
# list_conversations (S12.4)
# ---------------------------------------------------------------------------


class _CountThenPageDatabase:
    """Double that returns a canned count row on the first fetchrow, and
    canned page rows on fetch -- mirrors list_conversations'
    count-then-page query shape."""

    def __init__(self, *, total: int, rows: list[dict[str, Any]]) -> None:
        self._total = total
        self._rows = rows
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        return {"count": self._total}

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return self._rows

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    async def close(self) -> None:
        pass


def _conv_summary_row(
    conversation_id: str = "conv-1", **overrides: Any,
) -> dict[str, Any]:
    base = {
        "conversation_id": conversation_id,
        "status": "active",
        "channel": "widget",
        "visitor_id": "visitor-1",
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "summary": None,
        "message_count": 3,
    }
    base.update(overrides)
    return base


async def test_list_conversations_tenant_scoping_first_param_is_tenant_id() -> None:
    """MANDATORY isolation: tenant_id is the first bound param, for a
    tenant-A vs a distinct tenant-B claims object."""
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims_a = _claims("tenant-a", Role.CLIENT_ADMIN)
    claims_b = _claims("tenant-b", Role.CLIENT_AGENT)

    await list_conversations(db, claims_a)
    await list_conversations(db, claims_b)

    for query, args in [*db.fetchrow_calls, *db.fetch_calls]:
        assert "tenant_id" in query
        assert args[0] in ("tenant-a", "tenant-b")
        assert "$1" in query
        assert ":" not in query


async def test_list_conversations_rejects_global_caller() -> None:
    """MANDATORY: a PLATFORM_ADMIN (tenant_id=None) caller raises
    ValidationError, no query issued."""
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=0, rows=[])
    global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

    with pytest.raises(ValidationError):
        await list_conversations(db, global_claims)

    assert db.fetchrow_calls == []
    assert db.fetch_calls == []


async def test_list_conversations_window_filters_append_bound_clauses() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    started_from = datetime(2026, 1, 1, tzinfo=UTC)
    started_to = datetime(2026, 6, 1, tzinfo=UTC)

    await list_conversations(db, claims, started_from=started_from, started_to=started_to)

    query, args = db.fetch_calls[-1]
    assert "started_at >= $" in query.lower()
    assert "started_at < $" in query.lower()
    assert started_from in args
    assert started_to in args


async def test_list_conversations_status_and_channel_filters_bound_not_interpolated() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await list_conversations(db, claims, status="ended", channel="widget")

    query, args = db.fetch_calls[-1]
    assert "status = $" in query.lower()
    assert "channel = $" in query.lower()
    assert "ended" in args
    assert "widget" in args
    # values are bound, never string-interpolated into the SQL text itself
    assert "'ended'" not in query
    assert "'widget'" not in query


async def test_list_conversations_escalated_true_injects_exists() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await list_conversations(db, claims, escalated=True)

    query, _ = db.fetch_calls[-1]
    assert "EXISTS" in query
    assert "NOT EXISTS" not in query
    assert "decision = 'escalate'" in query
    assert "role = 'bot'" in query


async def test_list_conversations_escalated_false_injects_not_exists() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await list_conversations(db, claims, escalated=False)

    query, _ = db.fetch_calls[-1]
    assert "NOT EXISTS" in query


async def test_list_conversations_escalated_none_omits_clause() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await list_conversations(db, claims, escalated=None)

    query, _ = db.fetch_calls[-1]
    assert "EXISTS" not in query


async def test_list_conversations_visitor_ids_filter_appends_any_clause() -> None:
    """SR-9.3 J6/Scope §4: the visitor_ids filter used by the timeline
    fan-out appends exactly one ANY() clause, values bound not interpolated."""
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await list_conversations(db, claims, visitor_ids=["visitor-1", "visitor-2"])

    query, args = db.fetch_calls[-1]
    assert "visitor_id = any($" in query.lower()
    assert ["visitor-1", "visitor-2"] in args


async def test_list_conversations_visitor_ids_none_omits_clause() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await list_conversations(db, claims, visitor_ids=None)

    query, _ = db.fetch_calls[-1]
    assert "visitor_id = any" not in query.lower()


async def test_list_conversations_message_count_subselect_present() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row(message_count=7)])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    rows, total = await list_conversations(db, claims)

    query, _ = db.fetch_calls[-1]
    assert "count(*)" in query.lower()
    assert "message_count" in query.lower()
    assert rows[0].message_count == 7


async def test_list_conversations_returns_rows_and_total() -> None:
    from api.conversation_store.repository import ConversationSummaryRow, list_conversations

    db = _CountThenPageDatabase(total=42, rows=[_conv_summary_row(conversation_id="conv-9")])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    rows, total = await list_conversations(db, claims)

    assert total == 42
    assert isinstance(rows[0], ConversationSummaryRow)
    assert rows[0].conversation_id == "conv-9"


async def test_list_conversations_order_by_and_pagination() -> None:
    from api.conversation_store.repository import list_conversations

    db = _CountThenPageDatabase(total=1, rows=[_conv_summary_row()])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await list_conversations(db, claims, limit=10, offset=5)

    query, args = db.fetch_calls[-1]
    assert "order by c.started_at desc, c.conversation_id desc" in query.lower()
    assert "limit $" in query.lower()
    assert "offset $" in query.lower()
    assert 10 in args
    assert 5 in args


# ---------------------------------------------------------------------------
# get_messages source_count projection (SR-2)
# ---------------------------------------------------------------------------


def _msg_row_with_source_count(
    message_id: str, role: str, content: str, source_count: int, ts: datetime,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "role": role,
        "content": content,
        "intent": None,
        "confidence": None,
        "tokens": None,
        "created_at": ts,
        "source_count": source_count,
    }


async def test_get_messages_selects_source_count_via_coalesce_jsonb_array_length() -> None:
    """get_messages' SELECT projects source_count via
    coalesce(jsonb_array_length(sources), 0) -- NULL-safe -- and Message
    round-trips it."""
    now = datetime.now(UTC)
    rows = [_msg_row_with_source_count("m1", "bot", "The answer.", 3, now)]
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    messages = await get_messages(db, claims, "conv-1")

    assert len(messages) == 1
    assert messages[0].source_count == 3
    assert "coalesce(jsonb_array_length(sources), 0)" in db.last_sql.lower()


async def test_get_messages_source_count_zero_when_none_cited() -> None:
    now = datetime.now(UTC)
    rows = [_msg_row_with_source_count("m1", "user", "Hello", 0, now)]
    db = _RecordingDatabase(rows=rows)
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    messages = await get_messages(db, claims, "conv-1")

    assert messages[0].source_count == 0


async def test_message_source_count_defaults_to_zero_when_row_omits_it() -> None:
    """Regression guard: existing Message construction sites (get_message,
    get_window) that don't select source_count still produce a valid Message
    with source_count defaulting to 0 -- the dataclass default, not a KeyError."""
    from api.conversation_store.repository import Message

    msg = Message(
        message_id="m1",
        role="bot",
        content="hi",
        intent=None,
        confidence=None,
        tokens=None,
        created_at=datetime.now(UTC),
    )

    assert msg.source_count == 0


async def test_get_message_unaffected_by_source_count_still_returns_full_sources() -> None:
    """get_message is explicitly NOT changed by SR-2 -- it still returns the
    full sources list verbatim (the new /sources route needs it), and its
    Message.source_count defaults to 0 (get_message doesn't select it)."""
    row = _message_row_with_sources(
        sources=[{"doc_id": "d1", "chunk_id": "c1", "score": 0.9, "matched_by": ["vector"]}],
    )
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    msg = await get_message(db, claims, "conv-1", "msg-a")

    assert msg is not None
    assert msg.sources == [{"doc_id": "d1", "chunk_id": "c1", "score": 0.9, "matched_by": ["vector"]}]
    assert msg.source_count == 0
