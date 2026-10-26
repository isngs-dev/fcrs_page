"""A13 lead status, performance and exceptions on a REAL migrated Postgres (A3 fixture):
every status reached by its fact, exact metrics on a seeded tenant, meeting outcomes,
retry/dismiss, tenant isolation and RBAC."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role

from api.agents import planner
from api.agents.repository import add_suppression, record_tool_call, start_run
from api.auth.tokens import create_access_token
from api.email_agent import repository as email_repo
from api.leads.repository import create_lead
from api.scheduling.repository import create_event
from tests.integration.test_email_inbound_integration import _JWT, _claims, _contact, _tenant
from tests.integration.test_email_inbound_integration import env as _a3_env  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(_a3_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    await _tenant(_a3_env["db"], _claims(_a3_env["a"]))  # e-mail on, sender configured
    return _a3_env


def _cookie(tenant: str, role: Role = Role.CLIENT_ADMIN, subject: str = "u") -> dict[str, str]:
    token, _ = create_access_token(AuthClaims(subject=subject, role=role, tenant_id=tenant),
                                   secret=_JWT, ttl_seconds=300)
    return {"access_token": token}


async def _person(env: dict[str, Any], email: str, *, lead: bool = False) -> tuple[str, str]:
    """A contact with a plan. Returns (contact_id, plan_id)."""
    db, a = env["db"], _claims(env["a"])
    contact_id = await _contact(db, a, email=email)
    if lead:
        lead_id = await create_lead(db, a, visitor_id=f"v-{uuid4().hex[:6]}", name="L",
                                    email=email, phone=None, consent={}, source="widget")
        await db.execute("UPDATE contacts SET lead_id = $2 WHERE contact_id = $1", contact_id,
                         lead_id)
    await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual", goal=None,
                              now=datetime.now(UTC) + timedelta(days=1))
    plan_id = await db.fetchval("SELECT plan_id FROM outreach_plans WHERE contact_id = $1",
                                contact_id)
    return contact_id, str(plan_id)


async def _complete_first_step(env: dict[str, Any], plan_id: str, *, after: timedelta) -> None:
    await env["db"].execute(
        "UPDATE outreach_tasks SET status = 'completed', completed_at = "
        "(SELECT created_at FROM outreach_plans WHERE plan_id = $1) + $2 "
        "WHERE plan_id = $1 AND step_index = 0", plan_id, after)


async def _reply_from(env: dict[str, Any], contact_id: str, plan_id: str, email: str) -> None:
    db, a = env["db"], _claims(env["a"])
    thread, _ = await email_repo.get_or_create_thread(db, a, contact_id=contact_id,
                                                      plan_id=plan_id, subject="Hi")
    await email_repo.insert_message(
        db, a, thread_id=thread, rfc_message_id=f"<{uuid4().hex}@x>", in_reply_to=None,
        references=[], from_addr="team@acme.com", to_addr=email, subject="Hi", body_text="Hi",
        status="sent", band="answer", sources=[], task_id=uuid4().hex)
    await db.execute("UPDATE email_messages SET sent_at = now() WHERE thread_id = $1", thread)
    await email_repo.insert_inbound(
        db, a, thread_id=thread, rfc_message_id=f"<{uuid4().hex}@y>", in_reply_to=None,
        references=[], from_addr=email, to_addr="team@acme.com", subject="Re: Hi",
        body_text="Sounds good", reply_text="Sounds good", auth={})


async def test_every_status_is_reached_by_its_fact_and_priority_holds(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], _claims(env["a"])
    soon = datetime.now(UTC) + timedelta(days=3)

    await _person(env, "new@x.com")
    _, contacted = await _person(env, "contacted@x.com")
    await _complete_first_step(env, contacted, after=timedelta(minutes=5))
    engaged_id, engaged = await _person(env, "engaged@x.com")
    await _reply_from(env, engaged_id, engaged, "engaged@x.com")
    qualified_id, _ = await _person(env, "qualified@x.com", lead=True)
    await db.execute("UPDATE leads SET stage = 'qualified' WHERE lead_id = "
                     "(SELECT lead_id FROM contacts WHERE contact_id = $1)", qualified_id)
    await _person(env, "booked@x.com")
    await create_event(db, a, starts_at=soon, ends_at=soon + timedelta(minutes=30),
                       timezone="America/New_York", visitor_id=None, lead_id=None,
                       consent={}, email="booked@x.com", name="B")
    _, handoff = await _person(env, "handoff@x.com")
    await db.execute("UPDATE outreach_plans SET status = 'stopped', stop_reason = "
                     "'human_takeover' WHERE plan_id = $1", handoff)
    _, invalid = await _person(env, "invalid@x.com")
    await add_suppression(db, a, channel="email", value="invalid@x.com", reason="bounce",
                          source="test")
    await db.execute("UPDATE outreach_plans SET status = 'stopped' WHERE plan_id = $1", invalid)
    await _person(env, "nope@x.com")
    await add_suppression(db, a, channel="all", value="nope@x.com", reason="not_interested",
                          source="test")
    _, opted = await _person(env, "out@x.com")
    await add_suppression(db, a, channel="email", value="out@x.com", reason="unsubscribe",
                          source="test")
    await db.execute("UPDATE outreach_plans SET status = 'stopped' WHERE plan_id = $1", opted)
    # Priority: booked AND not interested -> not interested.
    await _person(env, "both@x.com")
    await create_event(db, a, starts_at=soon + timedelta(hours=1),
                       ends_at=soon + timedelta(hours=2), timezone="America/New_York",
                       visitor_id=None, lead_id=None, consent={}, email="both@x.com", name="C")
    await add_suppression(db, a, channel="all", value="both@x.com", reason="not_interested",
                          source="test")

    body = (await client.get("/admin/outreach/statuses", cookies=_cookie(env["a"]))).json()
    assert body["counts"] == {"new": 1, "contacted": 1, "engaged": 1, "qualified": 1,
                              "meeting_booked": 1, "human_handoff": 1, "invalid_contact": 1,
                              "not_interested": 2, "opted_out": 1}
    only = (await client.get("/admin/outreach/statuses?status=not_interested",
                             cookies=_cookie(env["a"]))).json()["contacts"]
    assert sorted(c["email"] for c in only) == ["both@x.com", "nope@x.com"]

    assert (await client.get("/admin/outreach/statuses",
                             cookies=_cookie(env["a"], Role.CLIENT_AGENT))).status_code == 403
    other = (await client.get("/admin/outreach/statuses", cookies=_cookie(env["b"]))).json()
    assert sum(other["counts"].values()) == 0


async def test_performance_numbers_on_a_seeded_tenant(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], _claims(env["a"])
    one, p1 = await _person(env, "one@x.com")
    two, p2 = await _person(env, "two@x.com")
    await _person(env, "three@x.com")
    await _person(env, "four@x.com")
    await _complete_first_step(env, p1, after=timedelta(minutes=4))
    await _complete_first_step(env, p2, after=timedelta(minutes=10))
    await _reply_from(env, one, p1, "one@x.com")   # 2 emailed, 1 replied
    thread, _ = await email_repo.get_or_create_thread(db, a, contact_id=two, plan_id=p2,
                                                      subject="Hi")
    await email_repo.insert_message(
        db, a, thread_id=thread, rfc_message_id="<t2@x>", in_reply_to=None, references=[],
        from_addr="team@acme.com", to_addr="two@x.com", subject="Hi", body_text="Hi",
        status="sent", band="answer", sources=[], task_id="t2")
    await db.execute("UPDATE email_messages SET sent_at = now() WHERE thread_id = $1", thread)
    await add_suppression(db, a, channel="email", value="two@x.com", reason="bounce",
                          source="test")
    past = datetime.now(UTC) - timedelta(hours=2)
    attended = await create_event(db, a, starts_at=past, ends_at=past + timedelta(minutes=30),
                                  timezone="America/New_York", visitor_id=None, lead_id=None,
                                  consent={}, email="one@x.com", name="One")
    await db.execute("INSERT INTO usage_costs (tenant_id, cost_id, kind, units, unit, "
                     "cost_micros) VALUES ($1, 'c1', 'email', 1, 'message', 8000000)", env["a"])

    admin = _cookie(env["a"])
    perf = (await client.get("/admin/outreach/performance?days=30", cookies=admin)).json()
    assert [m["event_id"] for m in perf["meetings_to_mark"]] == [attended.event_id]
    r = await client.post(f"/admin/outreach/meetings/{attended.event_id}:outcome",
                          json={"outcome": "attended"}, cookies=admin)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    perf = (await client.get("/admin/outreach/performance?days=30", cookies=admin)).json()
    assert (perf["leads"], perf["contacted"], perf["median_response_minutes"]) == (4, 2, 7.0)
    assert (perf["emails_sent"], perf["reply_rate"]) == (2, 0.5)
    assert (perf["meetings"], perf["lead_to_meeting"]) == (1, 0.25)
    assert (perf["attended"], perf["show_rate"], perf["bounce_rate"]) == (1, 1.0, 0.5)
    assert perf["costs"] == {"total_micros": 8000000, "per_lead_micros": 2000000,
                             "per_meeting_micros": 8000000}
    assert perf["meetings_to_mark"] == []

    agent = (await client.get("/admin/outreach/performance",
                              cookies=_cookie(env["a"], Role.CLIENT_AGENT))).json()
    assert agent["costs"] is None and agent["leads"] == 4
    future = datetime.now(UTC) + timedelta(days=2)
    upcoming = await create_event(db, a, starts_at=future, ends_at=future + timedelta(hours=1),
                                  timezone="America/New_York", visitor_id=None, lead_id=None,
                                  consent={}, email="one@x.com", name="One")
    early = await client.post(f"/admin/outreach/meetings/{upcoming.event_id}:outcome",
                              json={"outcome": "no_show"}, cookies=admin)
    assert early.status_code == 409  # only a past meeting
    as_agent = await client.post(f"/admin/outreach/meetings/{attended.event_id}:outcome",
                                 json={"outcome": "no_show"},
                                 cookies=_cookie(env["a"], Role.CLIENT_AGENT))
    assert as_agent.status_code == 404  # not their lead's meeting
    assert (await client.post(f"/admin/outreach/meetings/{attended.event_id}:outcome",
                              json={"outcome": "attended"},
                              cookies=_cookie(env["b"]))).status_code == 404


async def test_exceptions_list_retry_and_dismiss(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], _claims(env["a"])
    _, plan_id = await _person(env, "fail@x.com")
    failed, escalated = [str(r["task_id"]) for r in await db.fetch(
        "SELECT task_id FROM outreach_tasks WHERE plan_id = $1 ORDER BY step_index LIMIT 2",
        plan_id)]
    await db.execute("UPDATE outreach_tasks SET status = 'failed', attempts = 3, "
                     "last_error = 'SENDGRID_400' WHERE task_id = $1", failed)
    await db.execute("UPDATE outreach_tasks SET status = 'escalated', "
                     "last_error = 'NO_ANSWER_FOUND' WHERE task_id = $1", escalated)
    await db.execute("INSERT INTO agent_events (tenant_id, event_id, kind, payload, status, "
                     "attempts, last_error) VALUES ($1, 'ev-1', 'call_completed', '{}', "
                     "'failed', 5, 'RuntimeError')", env["a"])
    run_id = await start_run(db, a, agent="email")
    await record_tool_call(db, a, run_id, tool="compliance_gate", args_redacted={},
                           result_summary={"ok": False}, ok=False, error_code="NO_CONSENT")

    admin = _cookie(env["a"])
    found = (await client.get("/admin/outreach/exceptions", cookies=admin)).json()
    assert {(t["status"], t["last_error"]) for t in found["tasks"]} == {
        ("failed", "SENDGRID_400"), ("escalated", "NO_ANSWER_FOUND")}
    assert [e["event_id"] for e in found["dead_events"]] == ["ev-1"]
    assert found["blocked_7d"] == {"NO_CONSENT": 1}

    assert (await client.post(f"/admin/outreach/tasks/{failed}:retry",
                              cookies=admin)).status_code == 200
    row = await db.fetchrow("SELECT status, attempts FROM outreach_tasks WHERE task_id = $1",
                            failed)
    assert row is not None and tuple(row) == ("scheduled", 0)
    assert (await client.post(f"/admin/outreach/tasks/{escalated}:dismiss",
                              cookies=admin)).status_code == 200
    assert (await client.post("/admin/outreach/events/ev-1:retry", cookies=admin)
            ).status_code == 200
    assert await db.fetchval("SELECT status FROM agent_events WHERE event_id = 'ev-1'") == (
        "pending")
    left = (await client.get("/admin/outreach/exceptions", cookies=admin)).json()
    assert left["tasks"] == [] and left["dead_events"] == []

    b = _cookie(env["b"])
    assert (await client.post(f"/admin/outreach/tasks/{failed}:dismiss",
                              cookies=b)).status_code == 404
    assert (await client.get("/admin/outreach/exceptions",
                             cookies=_cookie(env["a"], Role.CLIENT_AGENT))).status_code == 403

    await db.execute("UPDATE outreach_tasks SET status = 'failed' WHERE task_id = $1", failed)
    await db.execute("UPDATE outreach_plans SET status = 'stopped' WHERE plan_id = $1", plan_id)
    ended = await client.post(f"/admin/outreach/tasks/{failed}:retry", cookies=admin)
    assert ended.status_code == 409 and ended.json()["error_code"] == "PLAN_NOT_ACTIVE"
