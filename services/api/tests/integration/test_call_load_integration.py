"""A9 H5/H6 concurrent-call load on a REAL migrated Postgres (Plivo and the model
faked, webhooks signed): the per-tenant call cap holds under parallel
dispatchers, and many live calls' turns are all answered. Our own handling time
per turn (model at 0 ms) is reported, not tuned for."""
from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any

import pytest

from api.agents import dispatch
from tests.integration.test_call_agent_integration import (
    _contact_with_call_task,
    _say,
    _webhook,
)
from tests.integration.test_call_agent_integration import env as _a6_env  # noqa: F401

pytestmark = pytest.mark.integration

_CALLS, _CAP, _DISPATCHERS, _TURNS = 30, 5, 4, 6


@pytest.fixture
async def env(_a6_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    await _a6_env["db"].execute("UPDATE tenant_call_agent_configs SET max_concurrent_calls = $1 "
                                "WHERE tenant_id = $2", _CAP, _a6_env["a"])
    return _a6_env


async def test_call_cap_holds_with_parallel_dispatchers_and_turns_keep_up(
    env: dict[str, Any],
) -> None:
    db, a = env["db"], env["a"]
    for i in range(_CALLS):
        await _contact_with_call_task(env, a, phone=f"20255501{i:02d}")

    await asyncio.gather(*(dispatch.dispatch_due_tasks(db, limit=10)
                           for _ in range(_DISPATCHERS)))

    assert env["plivo"].names().count("place_call") == _CAP
    assert await db.fetchval("SELECT count(*) FROM call_sessions WHERE tenant_id = $1", a) == _CAP
    rows = await db.fetch("SELECT status, attempts FROM outreach_tasks WHERE tenant_id = $1", a)
    assert sum(r["status"] == "pending" for r in rows) == _CAP          # dialled, awaiting hangup
    deferred = [r for r in rows if r["status"] == "scheduled"]
    assert len(deferred) == _CALLS - _CAP
    assert all(r["attempts"] == 0 for r in deferred)                    # deferral uses no attempt

    call_ids = [r["call_id"] for r in await db.fetch(
        "SELECT call_id FROM call_sessions WHERE tenant_id = $1", a)]
    for call_id in call_ids:
        assert (await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})).status_code == 200

    timings: list[float] = []

    async def converse(call_id: str) -> None:
        for _ in range(_TURNS):
            started = time.perf_counter()
            await _say(env, call_id, "How much does a new roof cost?")
            timings.append((time.perf_counter() - started) * 1000)

    await asyncio.gather(*(converse(c) for c in call_ids))

    assert len(timings) == _CAP * _TURNS
    turns = await db.fetchval("SELECT count(*) FROM call_transcript_turns WHERE tenant_id = $1 "
                              "AND speaker = 'caller'", a)
    assert turns == _CAP * _TURNS                                       # nothing dropped
    p50 = statistics.median(timings)
    p95 = statistics.quantiles(timings, n=20)[-1]
    print(f"\n[A9 load] {_CAP} live calls x {_TURNS} turns: handler p50={p50:.0f} ms "
          f"p95={p95:.0f} ms (model faked at 0 ms)")
    assert p95 < 2000  # generous: shared CI hardware; the model, not us, dominates real turns
