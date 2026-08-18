"""Unit tests for api.conversation_store.repository.close_idle_conversations.

Covers (SR-25 -- admin console "Active" tab never emptying because nothing
ever transitioned a conversation to status='ended'):
- An idle conversation (no message in > idle_minutes) is closed:
  status='ended', ended_at set to a real timestamp.
- A recently-active conversation (last message within idle_minutes) is left
  untouched.
- Exact-boundary behavior: idle_minutes ago exactly -> closed (<=, not <);
  one second under the threshold -> left active.
- A conversation with zero messages falls back to started_at as its last-
  activity reference point.
- Already-`ended` conversations are excluded (WHERE status='active' guards
  this) -- never re-processed / never appear in the returned list twice.
- System-scoped: no `claims` parameter (Celery Beat has no tenant context),
  mirrors scheduling.claim_due_reminders's own system-scoped shape.
- Multi-tenant: conversations from different tenants are swept in the same
  call (a Beat tick is not tenant-scoped).
- Idempotency: a second sweep immediately after the first returns nothing
  new (the first sweep's UPDATE already flipped every idle row).
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

_NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


class _StubDatabase:
    """In-memory stub modeling close_idle_conversations' UPDATE...RETURNING."""

    def __init__(self) -> None:
        self._conversations: dict[str, dict[str, Any]] = {}
        self._messages: list[dict[str, Any]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    def seed_conversation(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        status: str = "active",
        started_at: datetime = _NOW,
        ended_at: datetime | None = None,
    ) -> None:
        self._conversations[conversation_id] = {
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
        }

    def seed_message(self, *, tenant_id: str, conversation_id: str, created_at: datetime) -> None:
        self._messages.append(
            {"tenant_id": tenant_id, "conversation_id": conversation_id, "created_at": created_at}
        )

    def _last_activity(self, conv: dict[str, Any]) -> datetime:
        matches = [
            m["created_at"]
            for m in self._messages
            if m["tenant_id"] == conv["tenant_id"] and m["conversation_id"] == conv["conversation_id"]
        ]
        return max(matches) if matches else conv["started_at"]

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        q = query.strip().upper()

        if q.startswith("UPDATE CONVERSATIONS SET STATUS = 'ENDED'"):
            (idle_minutes,) = args
            cutoff = _NOW - timedelta(minutes=idle_minutes)
            closed: list[dict[str, Any]] = []
            for conv in self._conversations.values():
                if conv["status"] != "active":
                    continue
                if self._last_activity(conv) <= cutoff:
                    conv["status"] = "ended"
                    conv["ended_at"] = _NOW
                    closed.append(conv)
            return [
                {"conversation_id": c["conversation_id"], "tenant_id": c["tenant_id"]} for c in closed
            ]

        return []


@pytest.fixture
def stub_db() -> _StubDatabase:
    return _StubDatabase()


async def test_system_scoped_no_claims_param() -> None:
    """close_idle_conversations mirrors claim_due_reminders: no claims kwarg --
    Celery Beat has no tenant context, and this sweeps ALL tenants at once."""
    from api.conversation_store.repository import close_idle_conversations

    sig = inspect.signature(close_idle_conversations)
    assert "claims" not in sig.parameters


async def test_idle_conversation_is_closed(stub_db: _StubDatabase) -> None:
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(conversation_id="conv-1", tenant_id="tenant-a")
    stub_db.seed_message(
        tenant_id="tenant-a", conversation_id="conv-1", created_at=_NOW - timedelta(minutes=45)
    )

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert len(closed) == 1
    assert closed[0].conversation_id == "conv-1"
    assert closed[0].tenant_id == "tenant-a"
    assert stub_db._conversations["conv-1"]["status"] == "ended"
    assert stub_db._conversations["conv-1"]["ended_at"] == _NOW


async def test_recently_active_conversation_is_left_alone(stub_db: _StubDatabase) -> None:
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(conversation_id="conv-1", tenant_id="tenant-a")
    stub_db.seed_message(
        tenant_id="tenant-a", conversation_id="conv-1", created_at=_NOW - timedelta(minutes=5)
    )

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert closed == []
    assert stub_db._conversations["conv-1"]["status"] == "active"


async def test_boundary_exactly_at_idle_minutes_is_closed(stub_db: _StubDatabase) -> None:
    """<=, not < -- a conversation idle for EXACTLY idle_minutes is closed."""
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(conversation_id="conv-1", tenant_id="tenant-a")
    stub_db.seed_message(
        tenant_id="tenant-a", conversation_id="conv-1", created_at=_NOW - timedelta(minutes=30)
    )

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert len(closed) == 1


async def test_boundary_one_second_under_is_left_active(stub_db: _StubDatabase) -> None:
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(conversation_id="conv-1", tenant_id="tenant-a")
    stub_db.seed_message(
        tenant_id="tenant-a",
        conversation_id="conv-1",
        created_at=_NOW - timedelta(minutes=30) + timedelta(seconds=1),
    )

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert closed == []
    assert stub_db._conversations["conv-1"]["status"] == "active"


async def test_zero_message_conversation_falls_back_to_started_at(stub_db: _StubDatabase) -> None:
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(
        conversation_id="conv-1", tenant_id="tenant-a", started_at=_NOW - timedelta(minutes=45)
    )
    # No messages seeded at all.

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert len(closed) == 1
    assert stub_db._conversations["conv-1"]["status"] == "ended"


async def test_zero_message_recent_conversation_left_active(stub_db: _StubDatabase) -> None:
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(
        conversation_id="conv-1", tenant_id="tenant-a", started_at=_NOW - timedelta(minutes=5)
    )

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert closed == []


async def test_already_ended_conversation_is_excluded(stub_db: _StubDatabase) -> None:
    """WHERE status = 'active' guards this -- an already-ended row (however
    old its last message) is never re-processed / never appears in the
    returned list."""
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(
        conversation_id="conv-1",
        tenant_id="tenant-a",
        status="ended",
        ended_at=_NOW - timedelta(hours=2),
    )
    stub_db.seed_message(
        tenant_id="tenant-a", conversation_id="conv-1", created_at=_NOW - timedelta(hours=3)
    )

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert closed == []


async def test_multi_tenant_sweep_in_one_call(stub_db: _StubDatabase) -> None:
    """A single Beat tick sweeps every tenant at once -- not tenant-scoped."""
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(conversation_id="conv-a", tenant_id="tenant-a")
    stub_db.seed_message(
        tenant_id="tenant-a", conversation_id="conv-a", created_at=_NOW - timedelta(minutes=45)
    )
    stub_db.seed_conversation(conversation_id="conv-b", tenant_id="tenant-b")
    stub_db.seed_message(
        tenant_id="tenant-b", conversation_id="conv-b", created_at=_NOW - timedelta(minutes=60)
    )

    closed = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    tenant_ids = {c.tenant_id for c in closed}
    assert tenant_ids == {"tenant-a", "tenant-b"}


async def test_second_sweep_is_idempotent_returns_nothing_new(stub_db: _StubDatabase) -> None:
    from api.conversation_store.repository import close_idle_conversations

    stub_db.seed_conversation(conversation_id="conv-1", tenant_id="tenant-a")
    stub_db.seed_message(
        tenant_id="tenant-a", conversation_id="conv-1", created_at=_NOW - timedelta(minutes=45)
    )

    first = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]
    second = await close_idle_conversations(stub_db, idle_minutes=30)  # type: ignore[arg-type]

    assert len(first) == 1
    assert second == []
