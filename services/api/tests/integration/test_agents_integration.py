"""A1 agent foundations against a REAL, fully migrated Postgres (``alembic upgrade head``).

Stub DBs miss column drift and concurrency; everything here runs real SQL:
repositories + tenant isolation, every ComplianceGate code, the outbox
guarantee and exactly-once claims, the planner, the task dispatcher, tools and
the audit hash chain.

Requires ``TEST_DATABASE_URL`` pointing at a migrated, disposable database
(never ``DATABASE_URL``). Agent tables are truncated before each test.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import AppException, ConflictError, NotFoundError, ValidationError

from api.agents import dispatch, events, executors, outreach_repository, planner, tools
from api.agents.compliance import check
from api.agents.executors import ExecutorResult
from api.agents.repository import (
    AgentSettings,
    add_consent,
    add_suppression,
    get_agent_settings,
    latest_consent,
    list_tool_calls,
    set_platform_paused,
    set_tenant_paused,
    start_run,
    upsert_agent_settings,
)
from api.audit.chain import record_platform_audit, seal_audit_chain, verify_audit_chain
from api.audit.repository import record_audit
from api.contacts.repository import add_identity, create_contact, set_contact_timezone

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)

# 2026-10-15 14:00 UTC = 10:00 in New York (inside the default 09:00-20:00 window).
_NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
_PLATFORM_ADMIN = AuthClaims(subject="root", role=Role.PLATFORM_ADMIN, tenant_id=None)


@pytest.fixture
async def db() -> Any:
    assert _TEST_DB_URL is not None
    database = await Database.connect(_TEST_DB_URL, min_size=1, max_size=8,
                                      statement_cache_size=0)
    await database.execute(
        "TRUNCATE agent_events, outreach_tasks, outreach_plans, outreach_campaigns, "
        "agent_tool_calls, agent_runs, contact_consents, suppression_list, agent_settings, "
        "usage_costs, audit_chain_heads, audit_events")
    await database.execute("UPDATE platform_flags SET value = 'false' WHERE key = 'outreach_paused'")
    yield database
    for channel in executors.registered_channels():
        executors.unregister(channel)
    await database.close()


def _claims(tenant_id: str | None = None) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN,
                      tenant_id=tenant_id or f"t-{uuid4().hex[:10]}")


async def _contact(db: Database, claims: AuthClaims, *, email: str = "Jane@Example.com",
                   phone: str = "(786) 823-3553", tz: str | None = "America/New_York") -> str:
    contact_id = await create_contact(db, claims, account_id=None, lead_id=None, name="Jane",
                                      email=email, phone=phone, consent={"granted": True})
    await add_identity(db, claims, contact_id, identity_type="email", identity_value=email)
    await add_identity(db, claims, contact_id, identity_type="phone", identity_value=phone)
    if tz:
        await set_contact_timezone(db, claims, contact_id, tz)
    return contact_id


async def _ready(db: Database, claims: AuthClaims, **contact: Any) -> str:
    """Tenant with both channels enabled + a contact with email AND call consent."""
    await upsert_agent_settings(db, claims, AgentSettings(enabled_channels=("call", "email")))
    contact_id = await _contact(db, claims, **contact)
    await add_consent(db, claims, contact_id, channel="email", status="granted",
                      basis="documented_lawful_basis", source="admin_form")
    await add_consent(db, claims, contact_id, channel="call", status="granted",
                      basis="express_written_consent", source="csv")
    return contact_id


async def _gate(db: Database, claims: AuthClaims, contact_id: str, channel: str,
                now: datetime = _NOW) -> Any:
    run_id = await start_run(db, claims, agent="orchestrator")
    return await check(db, claims, contact_id=contact_id, channel=channel,  # type: ignore[arg-type]
                       now=now, run_id=run_id)


# -- Repositories ----------------------------------------------------------------------


async def test_settings_default_then_upsert_and_isolated(db: Database) -> None:
    a, b = _claims(), _claims()
    assert await get_agent_settings(db, a) == AgentSettings()
    await upsert_agent_settings(db, a, AgentSettings(enabled_channels=("email",),
                                                      max_touches_per_contact=5))
    assert (await get_agent_settings(db, a)).max_touches_per_contact == 5
    assert await get_agent_settings(db, b) == AgentSettings()


async def test_consent_latest_wins_and_cross_tenant_contact_is_not_found(db: Database) -> None:
    a, b = _claims(), _claims()
    contact_id = await _contact(db, a)
    await add_consent(db, a, contact_id, channel="call", status="granted",
                      basis="express_written_consent", source="csv")
    await add_consent(db, a, contact_id, channel="call", status="revoked",
                      basis="express_written_consent", source="admin_form")
    latest = await latest_consent(db, a, contact_id, "call")
    assert latest is not None and latest.status == "revoked"
    await add_consent(db, a, contact_id, channel="call", status="granted",
                      basis="express_consent", source="call")
    latest = await latest_consent(db, a, contact_id, "call")
    assert latest is not None and latest.status == "granted"

    assert await latest_consent(db, b, contact_id, "call") is None
    with pytest.raises(NotFoundError):
        await add_consent(db, b, contact_id, channel="email", status="granted",
                          basis="documented_lawful_basis", source="admin_form")


async def test_suppression_is_idempotent_per_channel_and_normalised(db: Database) -> None:
    a = _claims()
    first, created = await add_suppression(db, a, channel="email", value=" Jane@Example.COM ",
                                           reason="unsubscribe", source="admin")
    again, created_again = await add_suppression(db, a, channel="email", value="jane@example.com",
                                                 reason="manual", source="admin")
    assert created and not created_again and first.suppression_id == again.suppression_id
    _, other_channel = await add_suppression(db, a, channel="all", value="jane@example.com",
                                             reason="manual", source="admin")
    assert other_channel
    with pytest.raises(ValidationError):
        await add_suppression(db, a, channel="call", value="123", reason="manual", source="x")


# -- ComplianceGate ----------------------------------------------------------------------


async def test_gate_passes_both_channels_and_traces(db: Database) -> None:
    a = _claims()
    contact_id = await _ready(db, a)
    run_id = await start_run(db, a, agent="orchestrator")
    result = await check(db, a, contact_id=contact_id, channel="call", now=_NOW, run_id=run_id)
    assert result.ok, result
    assert (await _gate(db, a, contact_id, "email")).ok
    calls = await list_tool_calls(db, a, run_id)
    assert calls[0]["tool"] == "compliance_gate" and calls[0]["ok"]


async def test_gate_codes(db: Database) -> None:
    a = _claims()
    contact_id = await _ready(db, a)

    await set_platform_paused(db, _PLATFORM_ADMIN, paused=True)
    await set_tenant_paused(db, a, paused=True)
    assert (await _gate(db, a, contact_id, "email")).code == "OUTREACH_PAUSED_PLATFORM"
    await set_platform_paused(db, _PLATFORM_ADMIN, paused=False)
    assert (await _gate(db, a, contact_id, "email")).code == "OUTREACH_PAUSED_TENANT"
    await set_tenant_paused(db, a, paused=False)

    await upsert_agent_settings(db, a, AgentSettings(enabled_channels=("email",)))
    assert (await _gate(db, a, contact_id, "call")).code == "CHANNEL_DISABLED"
    await upsert_agent_settings(db, a, AgentSettings(enabled_channels=("call", "email")))

    assert (await _gate(db, a, uuid4().hex, "email")).code == "CONTACT_NOT_FOUND"

    # Night-time in New York (02:00) -> window; retry_at = 09:00 local.
    night = await _gate(db, a, contact_id, "call", datetime(2026, 10, 15, 6, 0, tzinfo=UTC))
    assert night.code == "OUTSIDE_CALLING_WINDOW"
    assert night.retry_at == datetime(2026, 10, 15, 13, 0, tzinfo=UTC)

    await add_suppression(db, a, channel="call", value="+1 786 823 3553", reason="stop",
                          source="test")
    assert (await _gate(db, a, contact_id, "call")).code == "CONTACT_SUPPRESSED"
    assert (await _gate(db, a, contact_id, "email")).ok  # suppression is per channel

    other = await _contact(db, a, email="x@y.com", phone="2025550100")
    await add_consent(db, a, other, channel="call", status="granted",
                      basis="express_consent", source="csv")  # D5: not enough for AI calls
    assert (await _gate(db, a, other, "call")).code == "NO_CONSENT"
    assert (await _gate(db, a, other, "email")).code == "NO_CONSENT"

    no_tz = await _contact(db, a, email="tz@y.com", phone="2025550101", tz=None)
    await add_consent(db, a, no_tz, channel="call", status="granted",
                      basis="express_written_consent", source="csv")
    await add_consent(db, a, no_tz, channel="email", status="granted",
                      basis="documented_lawful_basis", source="admin_form")
    assert (await _gate(db, a, no_tz, "call")).code == "CONTACT_TIMEZONE_UNKNOWN"
    assert (await _gate(db, a, no_tz, "email")).ok  # email has no window

    await upsert_agent_settings(db, a, AgentSettings(enabled_channels=("call", "email"),
                                                      monthly_cost_cap_micros=1000))
    await db.execute("INSERT INTO usage_costs (tenant_id, cost_id, kind, units, unit, "
                     "cost_micros, created_at) VALUES ($1, 'c1', 'llm', 1, 'tok', 1000, $2)",
                     a.tenant_id, _NOW)
    assert (await _gate(db, a, no_tz, "email")).code == "COST_CAP_REACHED"

    blocked = await db.fetchval("SELECT count(*) FROM audit_events WHERE tenant_id = $1 "
                                "AND action = 'outreach.gate_blocked'", a.tenant_id)
    assert blocked >= 9


async def test_gate_cadence_touches_and_attempts(db: Database) -> None:
    a = _claims()
    contact_id = await _ready(db, a)
    outcome = await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual",
                                        goal=None, now=_NOW - timedelta(days=10))
    assert outcome.plan_id
    await db.execute("UPDATE outreach_tasks SET status = 'completed', completed_at = $2 "
                     "WHERE tenant_id = $1 AND step_index = 0", a.tenant_id,
                     _NOW - timedelta(hours=3))
    cadence = await _gate(db, a, contact_id, "email")
    assert cadence.code == "CADENCE_LIMIT"
    assert cadence.retry_at == _NOW + timedelta(hours=45)
    # Someone who just asked (a number given in the chat, a form) isn't held 48 h.
    run_id = await start_run(db, a, agent="orchestrator")
    asked = await check(db, a, contact_id=contact_id, channel="email", now=_NOW, run_id=run_id,
                        purpose="first_response")
    assert asked.code != "CADENCE_LIMIT"

    await db.execute("UPDATE outreach_tasks SET status = 'completed', completed_at = $2 "
                     "WHERE tenant_id = $1", a.tenant_id, _NOW - timedelta(days=5))
    assert (await _gate(db, a, contact_id, "email")).code == "MAX_TOUCHES"

    await upsert_agent_settings(db, a, AgentSettings(enabled_channels=("call", "email"),
                                                      max_touches_per_contact=10))
    await db.execute("UPDATE outreach_tasks SET attempts = 2 WHERE tenant_id = $1 "
                     "AND channel = 'call'", a.tenant_id)
    assert (await _gate(db, a, contact_id, "call")).code == "MAX_ATTEMPTS"


async def test_gate_error_propagates_never_ok(db: Database) -> None:
    a = _claims()
    contact_id = await _ready(db, a)
    await db.execute("DELETE FROM platform_flags WHERE key = 'outreach_paused'")
    try:
        with pytest.raises(RuntimeError):
            await _gate(db, a, contact_id, "email")
    finally:
        await db.execute("INSERT INTO platform_flags (key, value) VALUES ('outreach_paused', "
                         "'false')")


# -- Outbox ------------------------------------------------------------------------------


async def test_emit_in_rolled_back_transaction_leaves_no_event(db: Database) -> None:
    a = _claims()
    with pytest.raises(RuntimeError):
        async with db.acquire() as conn, conn.transaction():
            await events.emit(conn, a, "opt_out_received", {"contact_id": "c"})
            raise RuntimeError("caller's state change failed")
    assert await db.fetchval("SELECT count(*) FROM agent_events") == 0


async def test_concurrent_event_claims_take_each_event_once(db: Database) -> None:
    a = _claims()
    for _ in range(20):
        await events.emit(db, a, "human_takeover", {"contact_id": "c"})
    batches = await asyncio.gather(*(events.claim_due_events(db, limit=7) for _ in range(5)))
    ids = [e.event_id for batch in batches for e in batch]
    assert len(ids) == 20 and len(set(ids)) == 20


async def test_event_retry_backoff_then_dead_letter(db: Database) -> None:
    a = _claims()
    # outreach_requested for a contact id that doesn't exist -> CONTACT_NOT_FOUND (permanent)
    await events.emit(db, a, "outreach_requested", {"contact_id": "missing",
                                                     "kind": "outbound_manual"})
    counts = await dispatch.dispatch_events(db, limit=10)
    assert counts["dead"] == 1
    row = await db.fetchrow("SELECT status, last_error FROM agent_events")
    assert row is not None and (row["status"], row["last_error"]) == ("failed", "CONTACT_NOT_FOUND")

    # A transient (non-AppException) failure retries with backoff until attempt 5.
    await db.execute("TRUNCATE agent_events")
    await events.emit(db, a, "outreach_requested", {"kind": "outbound_manual"})  # KeyError
    for attempt in range(1, 6):
        await db.execute("UPDATE agent_events SET available_at = now() - interval '1 second'")
        await dispatch.dispatch_events(db, limit=10)
        row = await db.fetchrow("SELECT status, attempts, available_at > now() AS later "
                                "FROM agent_events")
        assert row is not None and row["attempts"] == attempt
        assert row["status"] == ("failed" if attempt == 5 else "pending")
        if attempt < 5:
            assert row["later"]


async def test_stop_condition_cancels_only_that_tenants_contact(db: Database) -> None:
    a, b = _claims(), _claims()
    ca, cb = await _ready(db, a), await _ready(db, b)
    for claims, cid in ((a, ca), (b, cb)):
        assert (await planner.create_plan(db, claims, contact_id=cid, kind="outbound_manual",
                                          goal=None, now=_NOW)).plan_id
    await events.emit(db, a, "opt_out_received", {"contact_id": ca})
    counts = await dispatch.dispatch_events(db, limit=10)
    assert counts["done"] == 1
    assert await outreach_repository.get_active_plan(db, a, ca) is None
    plan_b = await outreach_repository.get_active_plan(db, b, cb)
    assert plan_b is not None and {t.status for t in plan_b["tasks"]} == {"scheduled"}
    statuses = await db.fetch("SELECT DISTINCT status FROM outreach_tasks WHERE tenant_id = $1",
                              a.tenant_id)
    assert [r["status"] for r in statuses] == ["cancelled"]


# -- Planner ------------------------------------------------------------------------------


async def test_outreach_requested_event_creates_one_plan(db: Database) -> None:
    a = _claims()
    contact_id = await _contact(db, a)
    await upsert_agent_settings(db, a, AgentSettings(enabled_channels=("call", "email")))
    await add_consent(db, a, contact_id, channel="email", status="granted",
                      basis="documented_lawful_basis", source="admin_form")
    await events.emit(db, a, "outreach_requested",
                      {"contact_id": contact_id, "kind": "outbound_manual", "goal": "follow_up"})
    await events.emit(db, a, "outreach_requested",
                      {"contact_id": contact_id, "kind": "outbound_manual", "goal": "follow_up"})
    counts = await dispatch.dispatch_events(db, limit=10)
    assert counts["done"] == 2
    plan = await outreach_repository.get_active_plan(db, a, contact_id)
    assert plan is not None
    assert [t.channel for t in plan["tasks"]] == ["email", "email"]  # call dropped: no consent
    assert "NO_CONSENT" in [n["code"] for n in plan["policy_notes"]]
    notes = await db.fetch("SELECT last_error FROM agent_events ORDER BY created_at")
    assert sorted(str(r["last_error"]) for r in notes) == ["None", "PLAN_ALREADY_ACTIVE"]
    keys = await db.fetch("SELECT idempotency_key FROM outreach_tasks")
    assert len({r["idempotency_key"] for r in keys}) == 2

    with pytest.raises(ConflictError) as exc:
        await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual",
                                  goal=None, now=_NOW)
    assert exc.value.code == "PLAN_ALREADY_ACTIVE"


async def test_no_allowed_channel_creates_nothing(db: Database) -> None:
    a = _claims()
    contact_id = await _contact(db, a)
    outcome = await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual",
                                        goal=None, now=_NOW)
    assert outcome.plan_id is None and outcome.notes[-1].code == "NO_ALLOWED_CHANNEL"
    assert await db.fetchval("SELECT count(*) FROM outreach_plans") == 0


# -- Task dispatcher ------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, db: Database, claims: AuthClaims, task: Any, run_id: str
                       ) -> ExecutorResult:
        assert claims.tenant_id == task.tenant_id  # claims rebuilt from the row
        self.calls.append((str(claims.tenant_id), task.task_id))
        await asyncio.sleep(0.01)
        return ExecutorResult(status="completed", result={"sent": True})


async def test_unregistered_channel_tasks_are_never_claimed(db: Database) -> None:
    a = _claims()
    contact_id = await _ready(db, a)
    await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual", goal=None,
                              now=_NOW)
    counts = await dispatch.dispatch_due_tasks(db, limit=50, now=_NOW + timedelta(days=30))
    assert counts == {"claimed": 0}
    statuses = await db.fetch("SELECT DISTINCT status FROM outreach_tasks")
    assert [r["status"] for r in statuses] == ["scheduled"]


async def test_concurrent_dispatchers_execute_each_task_once(db: Database) -> None:
    recorder = _Recorder()
    executors.register("email", recorder)
    tenants = [_claims() for _ in range(3)]
    for claims in tenants:
        for i in range(4):
            cid = await _ready(db, claims, email=f"p{i}@x.com", phone=f"20255501{i:02d}")
            await planner.create_plan(db, claims, contact_id=cid, kind="outbound_manual",
                                      goal=None, now=_NOW)
    results = await asyncio.gather(*(dispatch.dispatch_due_tasks(db, limit=5, now=_NOW)
                                     for _ in range(4)))
    assert sum(r["claimed"] for r in results) == 12  # the d0 email of each of 12 plans
    task_ids = [t for _, t in recorder.calls]
    assert len(task_ids) == 12 and len(set(task_ids)) == 12
    rows = await db.fetch("SELECT tenant_id, status, attempts FROM outreach_tasks "
                          "WHERE step_index = 0")
    assert {(r["status"], r["attempts"]) for r in rows} == {("completed", 1)}


async def test_gate_block_cancels_or_reschedules_without_consuming_attempts(
    db: Database,
) -> None:
    recorder = _Recorder()
    executors.register("email", recorder)
    a = _claims()
    suppressed = await _ready(db, a, email="s@x.com", phone="2025550111")
    window = await _ready(db, a, email="w@x.com", phone="2025550112")
    for cid in (suppressed, window):
        await planner.create_plan(db, a, contact_id=cid, kind="outbound_manual", goal=None,
                                  now=_NOW)
    await add_suppression(db, a, channel="email", value="s@x.com", reason="unsubscribe",
                          source="test")
    # A completed touch 1 h ago on the second contact -> CADENCE_LIMIT (reschedulable).
    await db.execute(
        "UPDATE outreach_tasks t SET status = 'completed', completed_at = $2 FROM outreach_plans p "
        "WHERE p.tenant_id = t.tenant_id AND p.plan_id = t.plan_id AND p.contact_id = $1 "
        "AND t.step_index = 2", window, _NOW - timedelta(hours=1))
    await dispatch.dispatch_due_tasks(db, limit=10, now=_NOW)
    assert recorder.calls == []
    rows = {r["contact_id"]: r for r in await db.fetch(
        "SELECT p.contact_id, t.status, t.attempts, t.last_error, t.scheduled_at "
        "FROM outreach_tasks t JOIN outreach_plans p USING (tenant_id, plan_id) "
        "WHERE t.step_index = 0")}
    assert (rows[suppressed]["status"], rows[suppressed]["last_error"]) == (
        "cancelled", "CONTACT_SUPPRESSED")
    assert rows[window]["status"] == "scheduled" and rows[window]["attempts"] == 0
    assert rows[window]["last_error"] == "CADENCE_LIMIT"
    assert rows[window]["scheduled_at"] == _NOW + timedelta(hours=47)


async def test_reaper_returns_stale_in_progress_tasks(db: Database) -> None:
    a = _claims()
    contact_id = await _ready(db, a)
    await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual", goal=None,
                              now=_NOW)
    await db.execute("UPDATE outreach_tasks SET status = 'in_progress', "
                     "claimed_at = now() - interval '20 minutes' WHERE step_index = 0")
    assert (await dispatch.reap(db, lease_seconds=600))["tasks"] == 1
    row = await db.fetchrow("SELECT status, last_error FROM outreach_tasks WHERE step_index = 0")
    assert row is not None and (row["status"], row["last_error"]) == ("scheduled", "LEASE_EXPIRED")


# -- Tools -------------------------------------------------------------------------------------


async def test_tool_input_validation_traces_failure_and_raises(db: Database) -> None:
    a = _claims()
    run_id = await start_run(db, a, agent="orchestrator")
    with pytest.raises(ValidationError) as exc:
        await tools.invoke(db, a, run_id, "get_contact", {"contact_id": ""})
    assert exc.value.code == "TOOL_INPUT_INVALID"
    calls = await list_tool_calls(db, a, run_id)
    assert len(calls) == 1 and not calls[0]["ok"]


async def test_get_contact_tool_is_tenant_isolated_and_args_redacted(db: Database) -> None:
    a, b = _claims(), _claims()
    contact_id = await _ready(db, a)
    run_a = await start_run(db, a, agent="orchestrator")
    out = await tools.invoke(db, a, run_a, "get_contact", {"contact_id": contact_id})
    assert out["consents"]["call"] == {"status": "granted", "basis": "express_written_consent"}
    run_b = await start_run(db, b, agent="orchestrator")
    with pytest.raises(NotFoundError):
        await tools.invoke(db, b, run_b, "get_contact", {"contact_id": contact_id})

    with pytest.raises(AppException):
        await tools.invoke(db, a, run_a, "log_activity", {
            "contact_id": contact_id, "type": "agent_note",
            "summary": "Spoke to jane@example.com on 786-823-3553"})
    stored = await db.fetchval("SELECT args_redacted::text FROM agent_tool_calls "
                               "WHERE tool = 'log_activity'")
    assert "jane@" not in stored and "823-3553" not in stored


# -- Audit chain -----------------------------------------------------------------------------


async def test_audit_chain_seals_verifies_and_detects_tampering(db: Database) -> None:
    a = _claims()
    for i in range(5):
        await record_audit(db, a, action="test.action", target_id=str(i), metadata={"i": i})
    report = await verify_audit_chain(db, a)
    assert report.rows_checked == 0 and report.unsealed == 5

    assert await seal_audit_chain(db) == 5
    report = await verify_audit_chain(db, a)
    assert report.ok and report.rows_checked == 5 and report.unsealed == 0

    await record_audit(db, a, action="test.action", target_id="later")
    await seal_audit_chain(db)
    assert (await verify_audit_chain(db, a)).rows_checked == 6

    await db.execute("UPDATE audit_events SET metadata = '{\"i\": 99}' "
                     "WHERE tenant_id = $1 AND chain_seq = 3", a.tenant_id)
    report = await verify_audit_chain(db, a)
    assert not report.ok and report.broken_at == 3


async def test_audit_chain_detects_deleted_rows(db: Database) -> None:
    a = _claims()
    for i in range(3):
        await record_audit(db, a, action="test.action", target_id=str(i))
    await seal_audit_chain(db)
    await db.execute("DELETE FROM audit_events WHERE tenant_id = $1 AND chain_seq = 3",
                     a.tenant_id)
    report = await verify_audit_chain(db, a)
    assert not report.ok and report.broken_at == 3


async def test_concurrent_writers_and_sealers_form_one_valid_chain(db: Database) -> None:
    a, b = _claims(), _claims()

    async def writer(claims: AuthClaims, n: int) -> None:
        for i in range(n):
            await record_audit(db, claims, action="test.concurrent", target_id=str(i))

    await asyncio.gather(writer(a, 30), writer(a, 30), writer(b, 20),
                         seal_audit_chain(db), seal_audit_chain(db))
    await asyncio.gather(seal_audit_chain(db), seal_audit_chain(db), seal_audit_chain(db))
    ra, rb = await verify_audit_chain(db, a), await verify_audit_chain(db, b)
    assert ra.ok and ra.rows_checked == 60 and ra.unsealed == 0
    assert rb.ok and rb.rows_checked == 20


async def test_platform_audit_is_chained_under_the_reserved_tenant(db: Database) -> None:
    with pytest.raises(AppException):
        await record_platform_audit(db, _claims(), action="outreach.platform_paused")
    await record_platform_audit(db, _PLATFORM_ADMIN, action="outreach.platform_paused")
    await seal_audit_chain(db)
    report = await verify_audit_chain(db, _claims("__platform__"))
    assert report.ok and report.rows_checked == 1
