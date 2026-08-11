"""Unit tests for api.leads.assignment (SR-20 D1/D3/D4) -- select_next_agent
and assign_lead_fail_open, the sprint's highest-risk piece.

Covers:
- Disabled (default) -> returns None, no agent query issued.
- Enabled, empty pool -> returns None (D3: the ordinary, non-error case).
- Enabled, pool present -> rotation advances in order and cycles back
  (D4: real round-robin, asserted over >= 2 full cycles).
- Only active CLIENT_ADMIN/CLIENT_AGENT rows are ever candidates (D4/M9)
  -- verified via the exact SQL predicate issued.
- The rotation cursor is advanced via a single atomic UPDATE ... RETURNING
  (D4) -- never a read-then-write across two statements.
- Global (PLATFORM_ADMIN) callers are rejected.
- assign_lead_fail_open: an exception anywhere inside select_next_agent
  or the assignment UPDATE is caught, logged at WARNING with
  event/tenant_id/lead_id/reason, and never propagates (D3 -- the single
  most important behavior in this sprint).
- assign_lead_fail_open: on success, the lead's assigned_agent_id is
  actually persisted via a parameterized UPDATE.
- Concurrency: two simultaneous calls to select_next_agent against a real
  Postgres-shaped atomic UPDATE ... RETURNING never return the same agent
  (integration-level proof lives in tests/integration; this unit test
  proves the SQL shape is a single statement, not read-then-write).
"""
from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import patch

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.leads.assignment import assign_lead_fail_open, select_next_agent

_TEST_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


@pytest.fixture(autouse=True)
def _env() -> Any:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        yield


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="visitor-1", role=role, tenant_id=tenant_id)


class _FakeDatabase:
    """Fake DB driving select_next_agent's three possible queries:

    1. SELECT round_robin_enabled FROM tenant_assignment_configs (config read)
    2. The atomic candidate-pool query (SELECT id FROM users WHERE ... ORDER BY id)
    3. UPDATE tenant_assignment_configs ... RETURNING last_assigned_agent_id
       (the atomic cursor advance)
    """

    def __init__(
        self,
        *,
        round_robin_enabled: bool,
        active_agent_ids: list[str],
        cursor_sequence: list[str | None] | None = None,
        raise_on_fetch: bool = False,
    ) -> None:
        self.round_robin_enabled = round_robin_enabled
        self.active_agent_ids = active_agent_ids
        self._cursor_sequence = cursor_sequence or []
        self._cursor_calls = 0
        self.raise_on_fetch = raise_on_fetch
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if self.raise_on_fetch:
            raise RuntimeError("simulated assignment failure")
        q = query.strip().upper()
        if q.startswith("SELECT ROUND_ROBIN_ENABLED"):
            return {
                "round_robin_enabled": self.round_robin_enabled,
                "last_assigned_agent_id": None,
            }
        if "UPDATE TENANT_ASSIGNMENT_CONFIGS" in q and "RETURNING" in q:
            # Atomic (possibly CTE-prefixed) UPDATE ... RETURNING the
            # newly-advanced cursor value.
            idx = self._cursor_calls
            self._cursor_calls += 1
            next_agent = (
                self._cursor_sequence[idx]
                if idx < len(self._cursor_sequence)
                else None
            )
            return {"last_assigned_agent_id": next_agent}
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return [{"id": agent_id} for agent_id in self.active_agent_ids]

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 1"


# -- select_next_agent: disabled / empty pool (D3 ordinary cases) ------------


async def test_disabled_returns_none_and_issues_no_candidate_query() -> None:
    db = _FakeDatabase(round_robin_enabled=False, active_agent_ids=["agent-a", "agent-b"])
    claims = _claims("tenant-1")

    result = await select_next_agent(db, claims)

    assert result is None
    # No pool query, no cursor UPDATE -- disabled short-circuits entirely.
    assert not db.fetch_calls
    assert not any(
        "UPDATE TENANT_ASSIGNMENT_CONFIGS" in c[0].strip().upper()
        for c in db.fetchrow_calls
    )


async def test_enabled_empty_pool_returns_none_ordinary_case() -> None:
    """Zero active agents -> None, no exception (D3: this is the ordinary case)."""
    db = _FakeDatabase(round_robin_enabled=True, active_agent_ids=[])

    result = await select_next_agent(db, _claims("tenant-1"))

    assert result is None


async def test_global_caller_rejected() -> None:
    db = _FakeDatabase(round_robin_enabled=True, active_agent_ids=["agent-a"])
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await select_next_agent(db, claims)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# -- select_next_agent: candidate pool predicate (D4/M9) ---------------------


async def test_candidate_pool_predicate_is_active_admin_or_agent_only() -> None:
    db = _FakeDatabase(
        round_robin_enabled=True,
        active_agent_ids=["agent-a"],
        cursor_sequence=["agent-a"],
    )

    await select_next_agent(db, _claims("tenant-1"))

    assert db.fetch_calls, "expected a candidate-pool query"
    sql, params = db.fetch_calls[0]
    upper = sql.upper()
    assert "ACTIVE" in upper
    assert "TRUE" in upper or "$" in upper  # active=true, parameterized or literal
    assert "CLIENT_ADMIN" in upper
    assert "CLIENT_AGENT" in upper
    assert "PLATFORM_ADMIN" not in upper
    assert params[0] == "tenant-1"


# -- select_next_agent: rotation advances atomically (D4) --------------------


async def test_rotation_advance_is_single_atomic_update_returning() -> None:
    """The cursor advance must be ONE UPDATE ... RETURNING statement -- never
    a read followed by a separate write (the read-then-write race D4 forbids).
    """
    db = _FakeDatabase(
        round_robin_enabled=True,
        active_agent_ids=["agent-a", "agent-b"],
        cursor_sequence=["agent-a"],
    )

    await select_next_agent(db, _claims("tenant-1"))

    # The cursor advance (the statement containing the UPDATE ... RETURNING
    # against tenant_assignment_configs, whether or not it is CTE-prefixed)
    # must be exactly ONE fetchrow round-trip.
    update_calls = [
        c
        for c in db.fetchrow_calls
        if "UPDATE TENANT_ASSIGNMENT_CONFIGS" in c[0].upper() and "RETURNING" in c[0].upper()
    ]
    assert len(update_calls) == 1, "cursor advance must be exactly one UPDATE...RETURNING call"
    # Exactly two fetchrow round-trips total: (1) the config read
    # (round_robin_enabled -- also happens to select last_assigned_agent_id
    # as a column, which is fine, it's ONE read of the whole config row) and
    # (2) the atomic cursor-advance UPDATE...RETURNING. No THIRD round-trip
    # that separately re-reads the cursor before writing it (the
    # read-then-write race D4 forbids).
    assert len(db.fetchrow_calls) == 2


async def test_rotation_cycles_over_two_full_cycles() -> None:
    """Consecutive calls assign agents in rotation, cycling back after the
    last (D4) -- asserted over >= 2 full cycles, not just 'not null'.
    """
    agents = ["agent-a", "agent-b", "agent-c"]
    # Simulate the DB-side rotation: each UPDATE...RETURNING call reports the
    # NEXT agent in sequence (what the real SQL's modulo-style advance would
    # produce), cycling twice through 3 agents = 6 calls.
    sequence = (agents * 2)[:6]
    db = _FakeDatabase(
        round_robin_enabled=True, active_agent_ids=agents, cursor_sequence=sequence
    )
    claims = _claims("tenant-1")

    chosen = [await select_next_agent(db, claims) for _ in range(6)]

    assert chosen == sequence


# -- assign_lead_fail_open: success path --------------------------------------


async def test_assign_lead_fail_open_persists_assignment_on_success() -> None:
    db = _FakeDatabase(
        round_robin_enabled=True,
        active_agent_ids=["agent-a"],
        cursor_sequence=["agent-a"],
    )
    claims = _claims("tenant-1")

    await assign_lead_fail_open(db, claims, lead_id="lead-1")

    update_lead_calls = [
        c for c in db.execute_calls if "UPDATE LEADS" in c[0].upper()
    ]
    assert len(update_lead_calls) == 1
    sql, params = update_lead_calls[0]
    assert "ASSIGNED_AGENT_ID" in sql.upper()
    assert "agent-a" in params


async def test_assign_lead_fail_open_disabled_does_not_touch_leads_table() -> None:
    db = _FakeDatabase(round_robin_enabled=False, active_agent_ids=["agent-a"])
    claims = _claims("tenant-1")

    await assign_lead_fail_open(db, claims, lead_id="lead-1")

    assert not any("UPDATE LEADS" in c[0].upper() for c in db.execute_calls)


# -- assign_lead_fail_open: THE critical fail-open test (D3) -----------------


async def test_assign_lead_fail_open_never_propagates_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The single most important test in this sprint (D3): when the
    assignment step raises for ANY reason, the exception must NEVER
    propagate out of assign_lead_fail_open, and a warning-level structured
    log with event/tenant_id/lead_id/reason must be written. No PII.

    ``assign_lead_fail_open`` opens its own ``with log_context(tenant_id=...)``
    around the warning log (so tenant_id -- a ContextVar-sourced field, per
    common.logging -- is present even outside an ambient request context).
    caplog's propagating handler captures the record while that context is
    still bound and formats via the real JsonFormatter through
    ``caplog.handler.format`` (not a reformat after the fact, which would
    run outside the block and lose tenant_id).
    """
    from common.logging import JsonFormatter

    caplog.handler.setFormatter(JsonFormatter())

    db = _FakeDatabase(
        round_robin_enabled=True, active_agent_ids=["agent-a"], raise_on_fetch=True
    )
    claims = _claims("tenant-1")

    with caplog.at_level(logging.WARNING):
        # Must not raise.
        await assign_lead_fail_open(db, claims, lead_id="lead-42")

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected a warning-level log on assignment failure"
    formatted = [caplog.handler.format(r) for r in warning_records]
    payloads = [json.loads(line) for line in formatted]
    payload = payloads[-1]
    assert payload.get("tenant_id") == "tenant-1"
    assert payload.get("lead_id") == "lead-42"
    assert payload.get("event") == "lead_auto_assignment_failed"
    assert payload.get("reason")
    # PII-safety: no lead name/email/phone ever appears in the payload --
    # assign_lead_fail_open never receives PII to begin with (only
    # lead_id), so this is structural, not just asserted text.
    assert "name" not in payload
    assert "email" not in payload
    assert "phone" not in payload


async def test_assign_lead_fail_open_swallows_arbitrary_exception_types() -> None:
    """Any exception -- not just a specific type -- must be caught (D3:
    'no active agents, a query failure, anything')."""

    class _ExplodingDatabase(_FakeDatabase):
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            raise ValueError("boom")

    db = _ExplodingDatabase(round_robin_enabled=True, active_agent_ids=["agent-a"])
    claims = _claims("tenant-1")

    # Must not raise, regardless of exception type.
    await assign_lead_fail_open(db, claims, lead_id="lead-1")


# -- SR-21: lead_auto_assigned feed emit (D3) --------------------------------


async def test_assign_lead_fail_open_emits_lead_auto_assigned_on_success() -> None:
    """A successful auto-assignment emits exactly one lead_auto_assigned
    feed event, AFTER the durable UPDATE, with ids only (no PII)."""
    from unittest.mock import AsyncMock, patch

    db = _FakeDatabase(
        round_robin_enabled=True,
        active_agent_ids=["agent-a"],
        cursor_sequence=["agent-a"],
    )
    claims = _claims("tenant-1")

    with patch(
        "api.leads.assignment.emit_event_safe", new=AsyncMock(return_value="event-1"),
    ) as mock_emit:
        await assign_lead_fail_open(db, claims, lead_id="lead-1")

    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "lead_auto_assigned"
    assert kwargs["category"] == "leads"
    assert kwargs["target_id"] == "lead-1"
    assert kwargs["payload"] == {"lead_id": "lead-1", "assigned_agent_id": "agent-a"}


async def test_assign_lead_fail_open_no_emit_when_disabled() -> None:
    """No feed event when round-robin is disabled -- nothing was assigned."""
    from unittest.mock import AsyncMock, patch

    db = _FakeDatabase(round_robin_enabled=False, active_agent_ids=["agent-a"])
    claims = _claims("tenant-1")

    with patch(
        "api.leads.assignment.emit_event_safe", new=AsyncMock(return_value="event-1"),
    ) as mock_emit:
        await assign_lead_fail_open(db, claims, lead_id="lead-1")

    mock_emit.assert_not_awaited()


async def test_assign_lead_fail_open_still_succeeds_when_feed_emit_raises() -> None:
    """MANDATORY (D2): a feed-insert failure must never break the
    assignment -- the lead's assigned_agent_id is still persisted."""
    from unittest.mock import AsyncMock, patch

    db = _FakeDatabase(
        round_robin_enabled=True,
        active_agent_ids=["agent-a"],
        cursor_sequence=["agent-a"],
    )
    claims = _claims("tenant-1")

    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
    ):
        # Must not raise.
        await assign_lead_fail_open(db, claims, lead_id="lead-1")

    update_lead_calls = [c for c in db.execute_calls if "UPDATE LEADS" in c[0].upper()]
    assert len(update_lead_calls) == 1
    assert "agent-a" in update_lead_calls[0][1]
