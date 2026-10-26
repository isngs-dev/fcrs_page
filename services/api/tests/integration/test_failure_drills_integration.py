"""A9 H7 failure drills on a REAL migrated Postgres: break one dependency at a time.
Each must fail EXPLICITLY (a recorded status + error code, nothing invented) and
recover by retry where retrying makes sense.

Already drilled elsewhere (and listed in specs/runbook.md): SendGrid 5xx retry with
the same Message-ID (test_email_agent_integration), outbox backoff -> dead-letter,
stale-lease reaper, exactly-once claims (test_agents_integration), duplicate inbound
mail stored once (test_email_inbound_integration), parallel dispatchers vs the call
cap (test_call_load_integration).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from common.errors import ValidationError

from api.agents import dispatch
from api.agents.executors import TransientExecutorError
from api.call_agent import script
from api.call_agent import service as call_service
from api.call_agent.telephony import CallProviderError
from api.email_agent import repository as email_repo
from api.email_agent import service as email_service
from api.llm.provider import LLMError
from tests.integration.test_call_agent_integration import (
    _contact_with_call_task,
    _dial,
    _say,
    _webhook,
)
from tests.integration.test_call_agent_integration import env as _a6_env  # noqa: F401
from tests.integration.test_email_agent_integration import (
    _claims,
    _plan_and_dispatch,
    _setup,
    _task,
)
from tests.integration.test_email_agent_integration import env as _a2_env  # noqa: F401

pytestmark = pytest.mark.integration


# -- Email -------------------------------------------------------------------------------


async def test_sendgrid_rejecting_fails_the_email_and_task_without_retry(
    _a2_env: dict[str, Any],  # noqa: F811 -- imported fixture
) -> None:
    env = _a2_env
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a)

    async def rejected(email: Any) -> Any:
        raise ValidationError("SendGrid rejected the request (HTTP 400).", code="SENDGRID_400")

    mailer.send = rejected
    now = datetime.now(UTC)
    await _plan_and_dispatch(db, a, contact_id, now)
    task = await _task(db)
    assert (task["status"], task["last_error"]) == ("failed", "SENDGRID_400")
    message = await email_repo.get_message_for_task(db, a, task["task_id"])
    assert message is not None and (message.status, message.last_error) == (
        "failed", "SENDGRID_400")
    counts = await dispatch.dispatch_due_tasks(db, limit=10, now=now + timedelta(hours=1))
    assert counts["claimed"] == 0  # never retried


async def test_model_outage_while_composing_retries_then_sends_once(
    _a2_env: dict[str, Any],  # noqa: F811 -- imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _a2_env
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a)
    working = email_service.compose
    outage = {"left": 1}

    async def flaky(*args: Any, **kw: Any) -> Any:
        if outage["left"]:
            outage["left"] -= 1
            raise LLMError("LLM request failed.")
        return await working(*args, **kw)

    monkeypatch.setattr(email_service, "compose", flaky)
    now = datetime.now(UTC)
    await _plan_and_dispatch(db, a, contact_id, now)
    task = await _task(db)
    assert (task["status"], task["attempts"], task["last_error"]) == (
        "scheduled", 1, "LLM_ERROR")
    assert await db.fetchval("SELECT count(*) FROM email_messages") == 0  # nothing invented
    assert mailer.sent == []

    await dispatch.dispatch_due_tasks(db, limit=10, now=now + timedelta(minutes=10))
    assert (await _task(db))["status"] == "completed"
    assert len(mailer.sent) == 1


async def test_model_outage_on_the_last_attempt_fails_explicitly(
    _a2_env: dict[str, Any],  # noqa: F811 -- imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _a2_env
    db, a = env["db"], _claims(env["a"])
    contact_id = await _setup(db, a)

    async def down(*args: Any, **kw: Any) -> Any:
        raise LLMError("LLM request failed.")

    monkeypatch.setattr(email_service, "compose", down)
    now = datetime.now(UTC)
    await _plan_and_dispatch(db, a, contact_id, now)
    for minutes in (10, 60, 240):
        await dispatch.dispatch_due_tasks(db, limit=10, now=now + timedelta(minutes=minutes))
    task = await _task(db)
    assert (task["status"], task["last_error"]) == ("failed", "LLM_ERROR")
    assert task["attempts"] == 3  # the email step's max_attempts


# -- Calls -------------------------------------------------------------------------------


async def test_plivo_down_retries_then_dials(
    _a6_env: dict[str, Any],  # noqa: F811 -- imported fixture
) -> None:
    env = _a6_env
    db, plivo, a = env["db"], env["plivo"], env["a"]
    _, task_id = await _contact_with_call_task(env, a)
    working = plivo.place_call

    async def unreachable(**kw: Any) -> Any:
        raise TransientExecutorError("Plivo unreachable: ConnectTimeout")

    plivo.place_call = unreachable
    await dispatch.dispatch_due_tasks(db, limit=10)
    row = await db.fetchrow("SELECT status, attempts, last_error FROM outreach_tasks "
                            "WHERE task_id = $1", task_id)
    assert row is not None and (row["status"], row["attempts"]) == ("scheduled", 1)
    assert row["last_error"] == "TransientExecutorError"
    assert await db.fetchval("SELECT status FROM call_sessions WHERE task_id = $1",
                             task_id) == "failed"  # the attempt is visible, not hidden

    plivo.place_call = working
    await dispatch.dispatch_due_tasks(db, limit=10, now=datetime.now(UTC) + timedelta(hours=1))
    assert await db.fetchval("SELECT status FROM outreach_tasks WHERE task_id = $1",
                             task_id) == "pending"  # dialled, waiting for the hangup
    assert plivo.names().count("place_call") == 1


async def test_plivo_refusing_fails_the_task_without_retry(
    _a6_env: dict[str, Any],  # noqa: F811 -- imported fixture
) -> None:
    env = _a6_env
    db, plivo, a = env["db"], env["plivo"], env["a"]
    _, task_id = await _contact_with_call_task(env, a)

    async def refused(**kw: Any) -> Any:
        raise CallProviderError("Plivo rejected the request (HTTP 401).", code="PLIVO_401")

    plivo.place_call = refused
    await dispatch.dispatch_due_tasks(db, limit=10)
    row = await db.fetchrow("SELECT status, last_error FROM outreach_tasks WHERE task_id = $1",
                            task_id)
    assert row is not None and tuple(row) == ("failed", "PLIVO_401")
    counts = await dispatch.dispatch_due_tasks(db, limit=10,
                                               now=datetime.now(UTC) + timedelta(days=1))
    assert counts["claimed"] == 0


async def test_model_down_mid_call_hands_the_caller_to_the_team(
    _a6_env: dict[str, Any],  # noqa: F811 -- imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _a6_env
    db = env["db"]

    async def down(*args: Any, **kw: Any) -> Any:
        raise LLMError("LLM request failed.")

    monkeypatch.setattr(call_service, "answer_from_knowledge", down)
    call_id, _ = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    said = await _say(env, call_id, "How much does a new roof cost?")
    assert script.TEAM_LINE in call_service.spoken_text(said)
    assert said.endswith("<Hangup/></Response>")  # an explicit goodbye, not dead air
    assert await db.fetchval("SELECT count(*) FROM notification_events "
                             "WHERE kind = 'agent_call_needs_human' AND tenant_id = $1",
                             env["a"]) == 1


async def test_duplicate_hangup_delivery_settles_once(
    _a6_env: dict[str, Any],  # noqa: F811 -- imported fixture
) -> None:
    env = _a6_env
    db = env["db"]
    call_id, task_id = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    params = {"CallUUID": "uuid-1", "Duration": "30", "CallStatus": "completed"}
    first = await _webhook(env, call_id, "hangup", params)
    again = await _webhook(env, call_id, "hangup", params)
    assert first.status_code == again.status_code == 200
    await dispatch.dispatch_events(db, limit=10)
    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT count(*) FROM agent_events "
                             "WHERE kind = 'call_completed'") == 1
    assert await db.fetchval("SELECT count(*) FROM usage_costs WHERE kind = 'telephony'") == 1
    assert await db.fetchval("SELECT status FROM outreach_tasks WHERE task_id = $1",
                             task_id) != "pending"


async def test_kill_switch_mid_flight_defers_without_using_attempts(
    _a6_env: dict[str, Any],  # noqa: F811 -- imported fixture
) -> None:
    env = _a6_env
    db, plivo, a = env["db"], env["plivo"], env["a"]
    _, task_id = await _contact_with_call_task(env, a)
    await db.execute("UPDATE platform_flags SET value = 'true' WHERE key = 'outreach_paused'")
    try:
        await dispatch.dispatch_due_tasks(db, limit=10)
        row = await db.fetchrow("SELECT status, attempts, last_error FROM outreach_tasks "
                                "WHERE task_id = $1", task_id)
        assert row is not None and tuple(row) == ("scheduled", 0, "OUTREACH_PAUSED_PLATFORM")
        assert "place_call" not in plivo.names()
    finally:
        await db.execute("UPDATE platform_flags SET value = 'false' "
                         "WHERE key = 'outreach_paused'")
    await dispatch.dispatch_due_tasks(db, limit=10, now=datetime.now(UTC) + timedelta(hours=1))
    assert plivo.names().count("place_call") == 1
