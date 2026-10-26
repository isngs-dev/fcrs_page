"""A12 event-driven follow-up on a REAL migrated Postgres: one follow-up after the last
reply, channel-only stops for bounces / wrong numbers / preferences, e-mail working
hours, campaign end dates."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from common.auth import AuthClaims, Role

from api.agents import dispatch, planner
from api.agents.events import emit
from api.agents.outreach_repository import end_campaigns_system
from api.agents.repository import AgentSettings, add_consent, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.contacts.repository import add_identity
from tests.integration.test_call_agent_integration import _dial, _say, _webhook
from tests.integration.test_call_agent_integration import env as _a6_env  # noqa: F401
from tests.integration.test_email_inbound_integration import (
    _JWT,
    _claims,
    _contact,
    _handle,
    _outreach,
    _post,
    _raw,
    _tenant,
)
from tests.integration.test_email_inbound_integration import env as _a3_env  # noqa: F401

pytestmark = pytest.mark.integration

_NY = ZoneInfo("America/New_York")


@pytest.fixture
def env(_a3_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    return _a3_env


async def _plans(env: dict[str, Any], contact_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in await env["db"].fetch(
        "SELECT plan_id, campaign_id, status, stop_reason FROM outreach_plans "
        "WHERE contact_id = $1 ORDER BY created_at", contact_id)]


async def _tasks(env: dict[str, Any], plan_id: str) -> list[tuple[str, str]]:
    return [(r["channel"], r["status"]) for r in await env["db"].fetch(
        "SELECT channel, status FROM outreach_tasks WHERE plan_id = $1 ORDER BY step_index",
        plan_id)]


async def _both_channels(env: dict[str, Any]) -> str:
    """Tenant A with e-mail + calls on, a contact with both consents and an active plan."""
    db, a = env["db"], _claims(env["a"])
    await _tenant(db, a)  # the e-mail sender (replies need it)
    await upsert_agent_settings(db, a, AgentSettings(
        enabled_channels=("call", "email"), email_mode="auto_send", physical_address="1 Main St",
        email_window_start=time(0), email_window_end=time(23, 59)))
    contact_id = await _contact(db, a)
    await add_identity(db, a, contact_id, identity_type="phone", identity_value="+12025550123")
    await add_consent(db, a, contact_id, channel="call", status="granted",
                      basis="express_written_consent", source="csv")
    await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual", goal=None,
                              now=datetime.now(UTC) + timedelta(hours=1))
    return contact_id


async def test_an_answered_reply_gets_one_follow_up_two_days_later(env: dict[str, Any]) -> None:
    db = env["db"]
    contact_id, sent = await _outreach(env)
    env["llm"].label = "question"
    await _post(env, _raw(body="Do you fix flat roofs?", in_reply_to=sent.message_id))
    assert await _handle(env) == ["question:answered"]
    first, follow = await _plans(env, contact_id)
    assert first["status"] == "stopped" and follow["status"] == "active"
    assert follow["campaign_id"] == first["campaign_id"]
    [(channel, when)] = [(r["channel"], r["scheduled_at"]) for r in await db.fetch(
        "SELECT channel, scheduled_at FROM outreach_tasks WHERE plan_id = $1", follow["plan_id"])]
    assert channel == "email"
    assert timedelta(hours=47) < when - datetime.now(UTC) <= timedelta(hours=48)

    await _post(env, _raw(body="And gutters too?", in_reply_to=sent.message_id))
    await _handle(env)
    plans = await _plans(env, contact_id)
    assert [p["status"] for p in plans] == ["stopped", "stopped", "active"]  # always one


async def test_the_follow_up_respects_the_touch_cap(env: dict[str, Any]) -> None:
    db, a = env["db"], _claims(env["a"])
    contact_id, sent = await _outreach(env)
    settings = AgentSettings(enabled_channels=("email",), email_mode="auto_send",
                             physical_address="1 Main St", max_touches_per_contact=1,
                             email_window_start=time(0), email_window_end=time(23, 59))
    await upsert_agent_settings(db, a, settings)
    env["llm"].label = "question"
    await _post(env, _raw(body="Do you fix flat roofs?", in_reply_to=sent.message_id))
    await _handle(env)
    assert [p["status"] for p in await _plans(env, contact_id)] == ["stopped"]


async def test_no_calls_keeps_the_email_steps(env: dict[str, Any]) -> None:
    contact_id = await _both_channels(env)
    [plan] = await _plans(env, contact_id)
    await env["db"].execute("INSERT INTO email_threads (tenant_id, thread_id, contact_id, plan_id, "
                            "subject) VALUES ($1, 'th-1', $2, $3, 'Hi')", env["a"], contact_id,
                            plan["plan_id"])
    env["llm"].label = "no_calls"
    await _post(env, _raw(body="Email is fine, please don't phone me",
                          to="reply+th-1@reply.acme.com"))
    await _handle(env)
    assert (await _plans(env, contact_id))[0]["status"] == "active"
    assert await _tasks(env, plan["plan_id"]) == [
        ("email", "scheduled"), ("call", "cancelled"), ("email", "scheduled")]


async def test_a_bounce_stops_email_only_and_the_permitted_call_continues(
    env: dict[str, Any],
) -> None:
    db, a = env["db"], _claims(env["a"])
    contact_id = await _both_channels(env)
    [plan] = await _plans(env, contact_id)
    await emit(db, a, "email_bounced", {"contact_id": contact_id})
    await dispatch.dispatch_events(db, limit=10)
    assert (await _plans(env, contact_id))[0]["status"] == "active"
    assert await _tasks(env, plan["plan_id"]) == [
        ("email", "cancelled"), ("call", "scheduled"), ("email", "cancelled")]

    email_only = await _contact(db, a, email="solo@example.com")  # e-mail consent only
    await planner.create_plan(db, a, contact_id=email_only, kind="outbound_manual", goal=None,
                              now=datetime.now(UTC) + timedelta(hours=1))
    await emit(db, a, "email_bounced", {"contact_id": email_only})
    await dispatch.dispatch_events(db, limit=10)
    [only] = await _plans(env, email_only)
    assert (only["status"], only["stop_reason"]) == ("stopped", "no_channel_left")


async def test_follow_ups_wait_for_working_hours_but_a_first_response_does_not(
    env: dict[str, Any],
) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    await _tenant(db, a)  # the sender; then the default 08:00-18:00 window
    await upsert_agent_settings(db, a, AgentSettings(
        enabled_channels=("email",), email_mode="auto_send", physical_address="1 Main St"))
    late = datetime.now(_NY).replace(hour=22, minute=0, second=0, microsecond=0).astimezone(UTC)
    contact_id = await _contact(db, a)
    await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual", goal=None,
                              now=late)
    await dispatch.dispatch_due_tasks(db, limit=10, now=late)
    row = await db.fetchrow("SELECT status, attempts, last_error, scheduled_at FROM outreach_tasks "
                            "WHERE step_index = 0")
    assert row is not None and (row["status"], row["attempts"], row["last_error"]) == (
        "scheduled", 0, "OUTSIDE_EMAIL_WINDOW")
    assert row["scheduled_at"].astimezone(_NY).hour == 8
    assert mailer.sent == []

    other = await _contact(db, a, email="night@example.com")
    await planner.create_plan(db, a, contact_id=other, kind="inbound_auto", goal=None, now=late)
    await dispatch.dispatch_due_tasks(db, limit=10, now=late + timedelta(minutes=6))
    debug = [dict(r) for r in await db.fetch(
        "SELECT t.status, t.last_error, t.scheduled_at, t.step_index, p.plan->>'kind' AS kind "
        "FROM outreach_tasks t JOIN outreach_plans p USING (tenant_id, plan_id) "
        "WHERE p.contact_id = $1", other)]
    assert [e.to for e in mailer.sent] == ["night@example.com"], debug  # they just asked


async def test_a_campaign_past_its_end_date_stops_its_plans(env: dict[str, Any]) -> None:
    db, client, a, b = env["db"], env["client"], env["a"], env["b"]
    contact_id, _ = await _outreach(env)
    [plan] = await _plans(env, contact_id)

    def cookie(tenant: str) -> dict[str, str]:
        token, _ = create_access_token(AuthClaims(subject="u", role=Role.CLIENT_ADMIN,
                                                  tenant_id=tenant), secret=_JWT, ttl_seconds=300)
        return {"access_token": token}

    path = f"/admin/outreach/campaigns/{plan['campaign_id']}"
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert (await client.patch(path, json={"ends_at": past}, cookies=cookie(b))).status_code == 404
    assert (await client.patch(path, json={"ends_at": "2026-12-31T23:59:00"},
                               cookies=cookie(a))).status_code == 422  # needs a timezone
    assert (await client.patch(path, json={"ends_at": past}, cookies=cookie(a))).status_code == 200
    assert await end_campaigns_system(db, now=datetime.now(UTC)) == 1
    [after] = await _plans(env, contact_id)
    assert (after["status"], after["stop_reason"]) == ("stopped", "campaign_ended")
    rows = (await client.get("/admin/outreach/campaigns", cookies=cookie(a))).json()
    assert rows[0]["status"] == "completed" and rows[0]["ends_at"]


async def test_a_wrong_number_stops_calls_only(
    _a6_env: dict[str, Any],  # noqa: F811 -- imported fixture
) -> None:
    env = _a6_env
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    await upsert_agent_settings(db, claims, AgentSettings(
        enabled_channels=("call", "email"), physical_address="1 Main St"))
    call_id, _ = await _dial(env, a)
    contact_id = await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                   call_id)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "You have the wrong number")
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "Duration": "10",
                                            "CallStatus": "completed"})
    await dispatch.dispatch_events(db, limit=10)
    status = await db.fetchval("SELECT status FROM outreach_plans WHERE contact_id = $1",
                               contact_id)
    assert status == "stopped"  # the A6 contact has call consent only: nothing left
    reason = await db.fetchval("SELECT stop_reason FROM outreach_plans WHERE contact_id = $1",
                               contact_id)
    assert reason == "no_channel_left"
