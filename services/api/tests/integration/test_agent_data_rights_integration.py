"""A9 H8/H9 on a REAL migrated Postgres (Plivo faked): recording retention, and a
contact's agent data export / erasure -- tenant-isolated, admin-only, audited,
consents + suppressions kept as proof, recordings deleted at Plivo first."""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role

from api.agents import dispatch
from api.agents.executors import TransientExecutorError
from api.agents.repository import add_suppression
from api.call_agent import service as call_service
from api.email_agent import repository as email_repo
from tests.integration.test_call_agent_integration import (
    _contact_with_call_task,
    _cookie,
    _say,
    _webhook,
)
from tests.integration.test_call_agent_integration import env as _a6_env  # noqa: F401

pytestmark = pytest.mark.integration

_REC = "https://media.plivo.com/v1/Account/MAX/Recording/{}.mp3"


def _claims(tenant: str) -> AuthClaims:
    return AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)


@pytest.fixture
async def env(_a6_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    await _a6_env["db"].execute("TRUNCATE email_messages, email_threads CASCADE")
    return _a6_env


async def _recorded_call(env: dict[str, Any], tenant: str, rec: str, *, days_old: int) -> str:
    _, task_id = await _contact_with_call_task(env, tenant, phone=f"202555{abs(hash(rec)) % 10000:04d}")
    await dispatch.dispatch_due_tasks(env["db"], limit=10)
    call_id = str(await env["db"].fetchval("SELECT call_id FROM call_sessions WHERE task_id = $1",
                                           task_id))
    await env["db"].execute(
        "UPDATE call_sessions SET recording_id = $2, recording_url = $3, status = 'completed', "
        "created_at = now() - make_interval(days => $4) WHERE call_id = $1",
        call_id, rec, _REC.format(rec), days_old)
    return call_id


# -- H8 retention -------------------------------------------------------------------------


async def test_recordings_past_retention_are_deleted_at_plivo_then_unlinked(
    env: dict[str, Any],
) -> None:
    db, plivo, a, b = env["db"], env["plivo"], env["a"], env["b"]
    old = await _recorded_call(env, a, "rec-old", days_old=100)
    fresh = await _recorded_call(env, a, "rec-new", days_old=10)
    await db.execute("UPDATE tenant_call_agent_configs SET recording_retention_days = 365 "
                     "WHERE tenant_id = $1", b)
    b_old = await _recorded_call(env, b, "rec-b", days_old=100)
    await db.execute("INSERT INTO call_transcript_turns (tenant_id, call_id, turn_index, speaker, "
                     "text) VALUES ($1, $2, 1, 'caller', 'hello')", a, old)

    assert await call_service.purge_expired_recordings(db) == {"deleted": 1, "failed": 0}
    deleted = [kw["recording_id"] for name, kw in plivo.calls if name == "delete_recording"]
    assert deleted == ["rec-old"]
    rows = {r["call_id"]: r["recording_url"] for r in await db.fetch(
        "SELECT call_id, recording_url FROM call_sessions")}
    assert rows[old] is None and rows[fresh] and rows[b_old]  # b keeps a year
    assert await db.fetchval("SELECT count(*) FROM call_transcript_turns WHERE call_id = $1",
                             old) >= 1  # transcripts stay with the contact


async def test_a_plivo_error_keeps_the_recording_for_the_next_run(env: dict[str, Any]) -> None:
    db, plivo = env["db"], env["plivo"]
    call_id = await _recorded_call(env, env["a"], "rec-x", days_old=100)
    working = plivo.delete_recording

    async def down(**kw: Any) -> None:
        raise TransientExecutorError("Plivo HTTP 503")

    plivo.delete_recording = down
    assert await call_service.purge_expired_recordings(db) == {"deleted": 0, "failed": 1}
    assert await db.fetchval("SELECT recording_url FROM call_sessions WHERE call_id = $1",
                             call_id)
    plivo.delete_recording = working
    assert await call_service.purge_expired_recordings(db) == {"deleted": 1, "failed": 0}


# -- H9 export / erase ----------------------------------------------------------------------


async def _contact_with_everything(env: dict[str, Any]) -> tuple[str, str]:
    """A contact with a plan, consent, a call + transcript + recording, an email and a
    suppression. Returns (contact_id, call_id)."""
    db, a = env["db"], env["a"]
    call_id = await _recorded_call(env, a, "rec-1", days_old=1)
    await db.execute("UPDATE call_sessions SET status = 'in_progress' WHERE call_id = $1",
                     call_id)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "How much does a new roof cost?")
    row = await db.fetchrow("SELECT contact_id, plan_id FROM call_sessions WHERE call_id = $1",
                            call_id)
    assert row is not None
    claims = _claims(a)
    thread, _ = await email_repo.get_or_create_thread(db, claims, contact_id=row["contact_id"],
                                                      plan_id=row["plan_id"], subject="Hi")
    await email_repo.insert_message(
        db, claims, thread_id=thread, rfc_message_id="<m@x>", in_reply_to=None, references=[],
        from_addr="team@acme.com", to_addr="jane@example.com", subject="Hi",
        body_text="Hello Jane", status="sent", band="answer", sources=[], task_id="t")
    await add_suppression(db, claims, channel="email", value="jane@example.com",
                          reason="unsubscribe", source="test")
    return str(row["contact_id"]), call_id


async def test_export_has_everything_and_is_admin_only_and_tenant_scoped(
    env: dict[str, Any],
) -> None:
    client, a, b = env["client"], env["a"], env["b"]
    contact_id, call_id = await _contact_with_everything(env)
    path = f"/admin/outreach/contacts/{contact_id}/agent-data"

    data = (await client.get(path, cookies=_cookie(a))).json()
    assert [p["status"] for p in data["plans"]] == ["active"] and data["plans"][0]["tasks"]
    assert [c["channel"] for c in data["consents"]] == ["call"]
    assert [s["value"] for s in data["suppressions"]] == ["jane@example.com"]
    assert data["emails"][0]["body_text"] == "Hello Jane"
    [call] = data["calls"]
    assert call["call_id"] == call_id and call["has_recording"]
    assert any(t["speaker"] == "caller" for t in call["transcript"])

    assert (await client.get(path, cookies=_cookie(a, Role.CLIENT_AGENT))).status_code == 403
    assert (await client.get(path, cookies=_cookie(b))).status_code == 404
    assert await env["db"].fetchval("SELECT count(*) FROM audit_events WHERE action = "
                                    "'outreach.agent_data_exported'") == 1


async def test_erase_deletes_agent_data_keeps_consent_and_suppression(env: dict[str, Any]) -> None:
    db, client, plivo, a, b = env["db"], env["client"], env["plivo"], env["a"], env["b"]
    contact_id, call_id = await _contact_with_everything(env)
    path = f"/admin/outreach/contacts/{contact_id}/agent-data"
    assert (await client.delete(path, cookies=_cookie(b))).status_code == 404
    assert (await client.delete(path, cookies=_cookie(a, Role.CLIENT_AGENT))).status_code == 403

    r = await client.delete(path, cookies=_cookie(a))
    assert r.status_code == 200, r.text
    assert r.json() == {"contact_id": contact_id, "plans": 1, "email_threads": 1, "calls": 1,
                        "recordings_deleted": 1}
    assert ("delete_recording", {"recording_id": "rec-1"}) in plivo.calls
    for table in ("outreach_plans", "outreach_tasks", "email_threads", "email_messages",
                  "call_sessions", "call_transcript_turns"):
        assert await db.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1",  # noqa: S608
                                 a) == 0, table
    assert await db.fetchval("SELECT count(*) FROM contact_consents WHERE contact_id = $1",
                             contact_id) == 1
    assert await db.fetchval("SELECT count(*) FROM suppression_list WHERE tenant_id = $1", a) == 1
    assert await db.fetchval("SELECT count(*) FROM audit_events WHERE action = "
                             "'outreach.agent_data_erased'") == 1
    assert call_id


async def test_erase_aborts_before_deleting_anything_when_plivo_fails(env: dict[str, Any]) -> None:
    db, client, plivo, a = env["db"], env["client"], env["plivo"], env["a"]
    contact_id, _ = await _contact_with_everything(env)

    async def down(**kw: Any) -> None:
        raise TransientExecutorError("Plivo HTTP 503")

    plivo.delete_recording = down
    r = await client.delete(f"/admin/outreach/contacts/{contact_id}/agent-data",
                            cookies=_cookie(a))
    assert r.status_code == 502 and r.json()["error_code"] == "RECORDING_DELETE_FAILED"
    assert await db.fetchval("SELECT count(*) FROM call_sessions WHERE tenant_id = $1", a) == 1
    assert await db.fetchval("SELECT count(*) FROM email_messages WHERE tenant_id = $1", a) == 1
