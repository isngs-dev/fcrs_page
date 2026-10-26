"""A8 console endpoints on a REAL migrated Postgres, reusing the A6 fixture:
overview, campaigns + stop, a lead's agent activity, human takeover, call
visibility per agent, and recording playback through the (faked) provider."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from common.auth import AuthClaims, Role

from api.agents import dispatch, planner
from api.agents.repository import add_consent
from api.contacts.repository import add_identity, create_contact, set_contact_timezone
from api.email_agent import repository as email_repo
from api.leads.repository import create_lead
from tests.integration.test_call_agent_integration import _cookie, _dial
from tests.integration.test_call_agent_integration import env as _a6_env  # noqa: F401

pytestmark = pytest.mark.integration

_REC = "https://media.plivo.com/v1/Account/MAX/Recording/rec-1.mp3"


def _claims(tenant: str) -> AuthClaims:
    return AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)


@pytest.fixture
async def env(_a6_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    await _a6_env["db"].execute("TRUNCATE email_messages, email_threads CASCADE")
    return _a6_env


async def _lead_with_plan(env: dict[str, Any], tenant: str) -> tuple[str, str]:
    """A lead whose contact has call consent, an active plan and a placed call."""
    db, claims = env["db"], _claims(tenant)
    lead_id = await create_lead(db, claims, visitor_id="v-1", name="Jane Doe",
                                email="jane@example.com", phone="+12025550123", consent={},
                                source="widget")
    cid = await create_contact(db, claims, account_id=None, lead_id=lead_id, name="Jane Doe",
                               email="jane@example.com", phone="+12025550123", consent={})
    await add_identity(db, claims, cid, identity_type="phone", identity_value="+12025550123")
    await set_contact_timezone(db, claims, cid, env["zone"])
    await add_consent(db, claims, cid, channel="call", status="granted",
                      basis="express_written_consent", source="csv")
    outcome = await planner.create_plan(db, claims, contact_id=cid, kind="outbound_manual",
                                        goal=None, now=datetime.now(UTC) - timedelta(days=3))
    assert outcome.plan_id
    await dispatch.dispatch_due_tasks(db, limit=10)
    thread_id, _ = await email_repo.get_or_create_thread(db, claims, contact_id=cid,
                                                         plan_id=outcome.plan_id, subject="Hi")
    await email_repo.insert_message(
        db, claims, thread_id=thread_id, rfc_message_id="<m1@x>", in_reply_to=None,
        references=[], from_addr="team@acme.com", to_addr="jane@example.com", subject="Hi",
        body_text="Hello Jane", status="draft", band="answer", sources=[], task_id="t-1")
    return lead_id, cid


async def test_overview_counts_and_costs_for_admins_only(env: dict[str, Any]) -> None:
    db, client, a, b = env["db"], env["client"], env["a"], env["b"]
    await _lead_with_plan(env, a)
    await db.execute("INSERT INTO usage_costs (tenant_id, cost_id, kind, units, unit, cost_micros) "
                     "VALUES ($1, 'c1', 'telephony', 2, 'minute', 120000)", a)

    got = (await client.get("/admin/outreach/overview", cookies=_cookie(a))).json()
    assert got["outreach_paused"] is False and got["platform_paused"] is False
    assert got["enabled_channels"] == ["call"]
    assert got["active_plans"] == 1 and got["drafts_awaiting"] == 1
    assert got["calls_7d"] == 1
    assert got["costs"] == {"month_micros": 120000, "cap_micros": None,
                            "by_kind": {"telephony": 120000}}

    agent = (await client.get("/admin/outreach/overview",
                              cookies=_cookie(a, Role.CLIENT_AGENT))).json()
    assert agent["costs"] is None and agent["active_plans"] == 1
    other = (await client.get("/admin/outreach/overview", cookies=_cookie(b))).json()
    assert other["active_plans"] == 0 and other["costs"]["month_micros"] == 0


async def test_campaigns_list_and_stop_is_tenant_scoped(env: dict[str, Any]) -> None:
    db, client, a, b = env["db"], env["client"], env["a"], env["b"]
    _, cid = await _lead_with_plan(env, a)
    rows = (await client.get("/admin/outreach/campaigns", cookies=_cookie(a))).json()
    assert len(rows) == 1 and rows[0]["plans_active"] == 1
    campaign_id = rows[0]["campaign_id"]
    assert (await client.get("/admin/outreach/campaigns",
                             cookies=_cookie(a, Role.CLIENT_AGENT))).status_code == 403
    assert (await client.get("/admin/outreach/campaigns", cookies=_cookie(b))).json() == []

    path = f"/admin/outreach/campaigns/{campaign_id}:stop"
    assert (await client.post(path, cookies=_cookie(b))).status_code == 404
    r = await client.post(path, cookies=_cookie(a))
    assert r.status_code == 200 and r.json()["plans_stopped"] == 1
    assert await db.fetchval("SELECT status FROM outreach_plans WHERE contact_id = $1",
                             cid) == "stopped"
    rows = (await client.get("/admin/outreach/campaigns", cookies=_cookie(a))).json()
    assert rows[0]["status"] == "cancelled" and rows[0]["plans_stopped"] == 1
    assert await db.fetchval("SELECT count(*) FROM audit_events WHERE action = "
                             "'outreach.campaign_stopped'") == 1


async def test_lead_activity_and_takeover(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], env["a"]
    lead_id, cid = await _lead_with_plan(env, a)
    path = f"/admin/outreach/leads/{lead_id}/activity"
    got = (await client.get(path, cookies=_cookie(a))).json()
    assert got["contact_id"] == cid
    assert got["plan"]["status"] == "active" and got["plan"]["tasks"]
    assert [c["channel"] for c in got["consents"]] == ["call"]
    assert [e["subject"] for e in got["emails"]] == ["Hi"]
    assert len(got["calls"]) == 1

    agent = _cookie(a, Role.CLIENT_AGENT)  # subject "u", not assigned yet
    assert (await client.get(path, cookies=agent)).status_code == 404
    await db.execute("UPDATE leads SET assigned_agent_id = 'u' WHERE lead_id = $1", lead_id)
    assert (await client.get(path, cookies=agent)).status_code == 200

    bare = await create_lead(db, _claims(a), visitor_id="v-2", name="No Contact", email=None,
                             phone=None, consent={}, source="widget")
    empty = (await client.get(f"/admin/outreach/leads/{bare}/activity", cookies=_cookie(a))).json()
    assert empty == {"contact_id": None, "plan": None, "consents": [], "emails": [], "calls": [],
                     "memory": []}

    r = await client.post(f"/admin/outreach/contacts/{cid}:takeover", cookies=agent)
    assert r.status_code == 200 and r.json()["event_id"] and r.json()["tasks_cancelled"] >= 1
    row = await db.fetchrow("SELECT status, stop_reason FROM outreach_plans WHERE contact_id = $1",
                            cid)
    assert tuple(row) == ("stopped", "human_takeover")  # at once, not after the dispatcher
    await dispatch.dispatch_events(db, limit=10)  # the event's own stop is a no-op now
    assert await db.fetchval("SELECT status FROM agent_events WHERE kind = 'human_takeover'")         == "done"
    assert (await client.post("/admin/outreach/contacts/nope:takeover",
                              cookies=_cookie(a))).status_code == 404


async def test_calls_are_hidden_from_agents_who_cant_see_the_contact(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], env["a"]
    call_id, _ = await _dial(env, a)
    agent = _cookie(a, Role.CLIENT_AGENT)
    assert (await client.get("/admin/outreach/calls", cookies=agent)).json() == []
    assert (await client.get(f"/admin/outreach/calls/{call_id}", cookies=agent)).status_code == 404

    await db.execute("UPDATE contacts SET owner_agent_id = 'u' WHERE contact_id = "
                     "(SELECT contact_id FROM call_sessions WHERE call_id = $1)", call_id)
    assert [c["call_id"] for c in (await client.get("/admin/outreach/calls",
                                                    cookies=agent)).json()] == [call_id]
    assert (await client.get(f"/admin/outreach/calls/{call_id}", cookies=agent)).status_code == 200

    await db.execute("UPDATE call_sessions SET contact_id = NULL WHERE call_id = $1", call_id)
    assert (await client.get("/admin/outreach/calls", cookies=agent)).json() == []
    assert len((await client.get("/admin/outreach/calls", cookies=_cookie(a))).json()) == 1


async def test_recording_streams_through_the_provider(env: dict[str, Any]) -> None:
    db, client, a, b = env["db"], env["client"], env["a"], env["b"]
    call_id, _ = await _dial(env, a)
    path = f"/admin/outreach/calls/{call_id}/recording"
    assert (await client.get(path, cookies=_cookie(a))).status_code == 404  # nothing recorded

    await db.execute("UPDATE call_sessions SET recording_id = 'rec-1', recording_url = $2 "
                     "WHERE call_id = $1", call_id, _REC)
    r = await client.get(path, cookies=_cookie(a))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"ID3-fake-audio"
    assert ("fetch_recording", {"url": _REC}) in env["plivo"].calls
    assert (await client.get(path, cookies=_cookie(b))).status_code == 404

    await db.execute("UPDATE call_sessions SET recording_url = 'https://evil.example.com/x.mp3' "
                     "WHERE call_id = $1", call_id)
    before = len(env["plivo"].calls)
    assert (await client.get(path, cookies=_cookie(a))).status_code == 404
    assert len(env["plivo"].calls) == before  # never fetched


async def test_email_config_keeps_the_event_key_unless_cleared(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.config import get_api_settings

    monkeypatch.setenv("EMAIL_LINK_SECRET", "l" * 40)
    get_api_settings.cache_clear()
    client, a = env["client"], env["a"]
    body = {"provider": "smtp", "from_address": "team@acme.com", "from_name": "Acme Roofing"}

    async def put(extra: dict[str, Any]) -> dict[str, Any]:
        r = await client.put("/admin/outreach/email-config", cookies=_cookie(a), json=body | extra)
        assert r.status_code == 200, r.text
        return dict(r.json())

    assert (await put({"event_webhook_public_key": "MFkwEwYHKoZIzj0CAQ"}))["has_event_key"]
    assert (await put({}))["has_event_key"]  # a settings save without the key keeps it
    assert not (await put({"event_webhook_public_key": ""}))["has_event_key"]
