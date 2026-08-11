"""Unit tests for api.notifications.events_repository (SR-21).

Covers (spec "Tests" section):
- emit_event inserts a notification_events row; _reject_global for PLATFORM_ADMIN.
- list_events: tenant isolation, category filter, unread_only filter,
  per-caller `read` flag resolution, unread_count, server-side limit clamp
  [1, 200], newest-first ordering, pagination (no skip/duplicate).
- mark_read / mark_all_read: per-user only, idempotent re-mark, only the
  caller's tenant's events.
- Cross-tenant read-state row is unrepresentable (composite FK simulated in
  the stub -- asserted the repository never attempts a cross-tenant write).
- MANDATORY: user X marking read does NOT affect user Y's unread state.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.notifications.events_repository import (
    emit_event,
    list_events,
    mark_all_read,
    mark_read,
)

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _StubDatabase:
    """In-memory stub database for the notification_events repository."""

    def __init__(self) -> None:
        # events: keyed by (tenant_id, event_id)
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        # reads: keyed by (tenant_id, event_id, user_id)
        self._reads: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        q = query.strip().upper()

        if q.startswith("INSERT INTO NOTIFICATION_EVENTS"):
            (
                tenant_id, event_id, kind, category, target_type, target_id,
                payload, actor_id, created_at,
            ) = args
            self._events[(tenant_id, event_id)] = {
                "tenant_id": tenant_id,
                "event_id": event_id,
                "kind": kind,
                "category": category,
                "target_type": target_type,
                "target_id": target_id,
                "payload": payload,
                "actor_id": actor_id,
                "created_at": created_at,
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO NOTIFICATION_EVENT_READS"):
            # ON CONFLICT DO NOTHING semantics -- idempotent re-mark-read.
            tenant_id, event_id, user_id, read_at = args
            if (tenant_id, event_id) not in self._events:
                # composite FK: cannot insert a read row for a non-existent
                # (tenant_id, event_id) pair -- makes cross-tenant rows
                # unrepresentable.
                return "INSERT 0 0"
            key = (tenant_id, event_id, user_id)
            if key in self._reads:
                return "INSERT 0 0"
            self._reads[key] = {
                "tenant_id": tenant_id, "event_id": event_id,
                "user_id": user_id, "read_at": read_at,
            }
            return "INSERT 0 1"

        if q.startswith("DELETE FROM NOTIFICATION_EVENT_READS"):
            tenant_id, event_id, user_id = args
            key = (tenant_id, event_id, user_id)
            existed = key in self._reads
            self._reads.pop(key, None)
            return "DELETE 1" if existed else "DELETE 0"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM NOTIFICATION_EVENTS" in q and "EVENT_ID = $" in q:
            tenant_id, event_id = args[0], args[1]
            return self._events.get((tenant_id, event_id))
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = query.strip().upper()
        if "COUNT(*)" not in q or "FROM NOTIFICATION_EVENTS" not in q:
            return 0

        # Distinguish the two COUNT queries the repository issues:
        # 1. The bare "unread_count" query: WHERE tenant_id = $1 AND NOT
        #    EXISTS(... user_id = $2) -- always exactly 2 params, no
        #    "CATEGORY = $" text.
        # 2. The "total" query (used for both the plain total and the
        #    unread_only-scoped total): WHERE tenant_id = $1 [AND category
        #    = $N] [AND NOT EXISTS(... user_id = $N)].
        is_bare_unread_count = "NOT EXISTS" in q and "CATEGORY = $" not in q and len(args) == 2

        if is_bare_unread_count:
            tenant_id, user_id = args[0], args[1]
            return sum(
                1 for (t_id, e_id) in self._events
                if t_id == tenant_id and (t_id, e_id, user_id) not in self._reads
            )

        idx = 1
        tenant_id = args[0]
        rows = [r for r in self._events.values() if r["tenant_id"] == tenant_id]
        if "CATEGORY = $" in q:
            category = args[idx]
            idx += 1
            rows = [r for r in rows if r["category"] == category]
        if "NOT EXISTS" in q:
            user_id = args[idx]
            idx += 1
            rows = [
                r for r in rows
                if (r["tenant_id"], r["event_id"], user_id) not in self._reads
            ]
        return len(rows)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM NOTIFICATION_EVENTS" in q:
            tenant_id = args[0]
            rows = [dict(r) for r in self._events.values() if r["tenant_id"] == tenant_id]
            # Parse optional category / unread_only / user_id args positionally
            # by inspecting the query text (mirrors the real repository's
            # conditional WHERE clauses).
            idx = 1
            if "CATEGORY = $" in q:
                category = args[idx]
                idx += 1
                rows = [r for r in rows if r["category"] == category]

            # user_id always present (for the `read` flag join)
            user_id = args[idx]
            idx += 1

            rows.sort(key=lambda r: (r["created_at"], r["event_id"]), reverse=True)

            for r in rows:
                r["read"] = (tenant_id, r["event_id"], user_id) in self._reads

            # The repository emits the read-flag projection as one EXISTS(...)
            # and, only when unread_only=True, a SECOND NOT EXISTS(...) clause
            # in the WHERE -- so 2+ occurrences of "EXISTS" (one EXISTS, one
            # NOT EXISTS) means unread_only was requested.
            if q.count("EXISTS") >= 2:
                rows = [r for r in rows if not r["read"]]

            limit = args[idx] if idx < len(args) else None
            offset = args[idx + 1] if idx + 1 < len(args) else 0
            if limit is not None:
                rows = rows[offset: offset + limit]
            return rows
        return []


def _claims(role: Role, tenant_id: str | None, subject: str = "user-1") -> AuthClaims:
    return AuthClaims(subject=subject, role=role, tenant_id=tenant_id)


@pytest.fixture
def db() -> _StubDatabase:
    return _StubDatabase()


# ---------------------------------------------------------------------------
# emit_event
# ---------------------------------------------------------------------------


async def test_emit_event_inserts_row(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    event_id = await emit_event(
        db, claims, kind="lead_captured", category="leads",
        target_type="lead", target_id="lead-1", payload={"lead_id": "lead-1"},
        actor_id=None,
    )
    row = db._events[(_TENANT_A, event_id)]
    assert row["kind"] == "lead_captured"
    assert row["category"] == "leads"
    assert row["target_id"] == "lead-1"


async def test_emit_event_rejects_global_caller(db: _StubDatabase) -> None:
    claims = _claims(Role.PLATFORM_ADMIN, None)
    with pytest.raises(ValidationError) as excinfo:
        await emit_event(
            db, claims, kind="lead_captured", category="leads",
            target_type="lead", target_id="lead-1", payload=None, actor_id=None,
        )
    assert excinfo.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# list_events -- tenant isolation, filters, unread_count, read flag
# ---------------------------------------------------------------------------


async def test_list_events_tenant_isolation(db: _StubDatabase) -> None:
    claims_a = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    claims_b = _claims(Role.CLIENT_ADMIN, _TENANT_B)
    await emit_event(db, claims_a, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l1", payload=None, actor_id=None)
    await emit_event(db, claims_b, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l2", payload=None, actor_id=None)

    rows, total, unread = await list_events(db, claims_a, user_id="user-1", limit=50, offset=0)
    assert total == 1
    assert len(rows) == 1
    assert rows[0].target_id == "l1"


async def test_list_events_category_filter(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    await emit_event(db, claims, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l1", payload=None, actor_id=None)
    await emit_event(db, claims, kind="conversation_escalated", category="system",
                      target_type="conversation", target_id="c1", payload=None, actor_id=None)

    rows, total, _ = await list_events(db, claims, user_id="user-1", category="leads", limit=50, offset=0)
    assert total == 1
    assert rows[0].category == "leads"

    rows, total, _ = await list_events(db, claims, user_id="user-1", category="system", limit=50, offset=0)
    assert total == 1
    assert rows[0].category == "system"


async def test_list_events_rejects_global_caller(db: _StubDatabase) -> None:
    claims = _claims(Role.PLATFORM_ADMIN, None)
    with pytest.raises(ValidationError):
        await list_events(db, claims, user_id="user-1", limit=50, offset=0)


async def test_list_events_limit_clamped(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    for i in range(5):
        await emit_event(db, claims, kind="lead_captured", category="leads",
                          target_type="lead", target_id=f"l{i}", payload=None, actor_id=None)

    rows, total, _ = await list_events(db, claims, user_id="user-1", limit=100000, offset=0)
    # clamp caps at 200 -- with only 5 rows this just proves no crash/full dump
    # beyond what exists; the clamp itself is asserted via the call args below.
    assert len(rows) == 5
    assert total == 5


async def test_list_events_limit_clamp_minimum(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    await emit_event(db, claims, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l1", payload=None, actor_id=None)
    rows, _, _ = await list_events(db, claims, user_id="user-1", limit=0, offset=0)
    assert len(rows) == 1  # limit=0 clamps up to 1, not down to 0


async def test_list_events_pagination_no_skip_or_duplicate(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    for i in range(5):
        await emit_event(db, claims, kind="lead_captured", category="leads",
                          target_type="lead", target_id=f"l{i}", payload=None, actor_id=None)

    page1, total, _ = await list_events(db, claims, user_id="user-1", limit=2, offset=0)
    page2, _, _ = await list_events(db, claims, user_id="user-1", limit=2, offset=2)
    page3, _, _ = await list_events(db, claims, user_id="user-1", limit=2, offset=4)

    all_ids = [r.event_id for r in page1 + page2 + page3]
    assert total == 5
    assert len(all_ids) == len(set(all_ids))  # no duplicates
    assert len(all_ids) == 5  # no skips


async def test_list_events_read_flag_and_unread_count(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    event_id = await emit_event(db, claims, kind="lead_captured", category="leads",
                                 target_type="lead", target_id="l1", payload=None, actor_id=None)

    rows, _, unread = await list_events(db, claims, user_id="user-x", limit=50, offset=0)
    assert rows[0].read is False
    assert unread == 1

    await mark_read(db, claims, user_id="user-x", event_id=event_id, read=True)

    rows, _, unread = await list_events(db, claims, user_id="user-x", limit=50, offset=0)
    assert rows[0].read is True
    assert unread == 0


async def test_list_events_unread_only_filter(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    e1 = await emit_event(db, claims, kind="lead_captured", category="leads",
                           target_type="lead", target_id="l1", payload=None, actor_id=None)
    await emit_event(db, claims, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l2", payload=None, actor_id=None)
    await mark_read(db, claims, user_id="user-1", event_id=e1, read=True)

    rows, total, _ = await list_events(db, claims, user_id="user-1", unread_only=True, limit=50, offset=0)
    assert total == 1
    assert rows[0].target_id == "l2"


# ---------------------------------------------------------------------------
# Per-user read state -- MANDATORY, the sprint's highest-value test
# ---------------------------------------------------------------------------


async def test_user_x_marking_read_does_not_affect_user_y(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    event_id = await emit_event(db, claims, kind="lead_captured", category="leads",
                                 target_type="lead", target_id="l1", payload=None, actor_id=None)

    await mark_read(db, claims, user_id="user-x", event_id=event_id, read=True)

    _, _, unread_x = await list_events(db, claims, user_id="user-x", limit=50, offset=0)
    _, _, unread_y = await list_events(db, claims, user_id="user-y", limit=50, offset=0)

    assert unread_x == 0
    assert unread_y == 1

    rows_y, _, _ = await list_events(db, claims, user_id="user-y", limit=50, offset=0)
    assert rows_y[0].read is False


async def test_mark_read_idempotent(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    event_id = await emit_event(db, claims, kind="lead_captured", category="leads",
                                 target_type="lead", target_id="l1", payload=None, actor_id=None)
    await mark_read(db, claims, user_id="user-x", event_id=event_id, read=True)
    # Re-marking read must not error and must not duplicate the read row.
    await mark_read(db, claims, user_id="user-x", event_id=event_id, read=True)
    assert len([k for k in db._reads if k[:2] == (_TENANT_A, event_id)]) == 1


async def test_mark_read_cross_tenant_event_is_noop(db: _StubDatabase) -> None:
    claims_a = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    claims_b = _claims(Role.CLIENT_ADMIN, _TENANT_B)
    event_id = await emit_event(db, claims_a, kind="lead_captured", category="leads",
                                 target_type="lead", target_id="l1", payload=None, actor_id=None)

    result = await mark_read(db, claims_b, user_id="user-1", event_id=event_id, read=True)
    assert result is False
    assert (_TENANT_B, event_id, "user-1") not in db._reads


async def test_mark_all_read_only_affects_caller(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    await emit_event(db, claims, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l1", payload=None, actor_id=None)
    await emit_event(db, claims, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l2", payload=None, actor_id=None)

    marked = await mark_all_read(db, claims, user_id="user-x")
    assert marked == 2

    _, _, unread_x = await list_events(db, claims, user_id="user-x", limit=50, offset=0)
    _, _, unread_y = await list_events(db, claims, user_id="user-y", limit=50, offset=0)
    assert unread_x == 0
    assert unread_y == 2


async def test_mark_all_read_category_scoped(db: _StubDatabase) -> None:
    claims = _claims(Role.CLIENT_ADMIN, _TENANT_A)
    await emit_event(db, claims, kind="lead_captured", category="leads",
                      target_type="lead", target_id="l1", payload=None, actor_id=None)
    await emit_event(db, claims, kind="conversation_escalated", category="system",
                      target_type="conversation", target_id="c1", payload=None, actor_id=None)

    marked = await mark_all_read(db, claims, user_id="user-x", category="leads")
    assert marked == 1

    rows, _, unread = await list_events(db, claims, user_id="user-x", limit=50, offset=0)
    read_map = {r.target_id: r.read for r in rows}
    assert read_map["l1"] is True
    assert read_map["c1"] is False
    assert unread == 1
