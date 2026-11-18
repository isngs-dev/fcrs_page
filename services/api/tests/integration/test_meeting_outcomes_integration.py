"""Plan 2.1 on a REAL migrated Postgres (A3 fixture): marking a meeting "no-show" starts the
recovery sequence for the person who booked it; "attended" afterwards stops it. Per tenant,
over the console route (staff only)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from common.auth import AuthClaims, Role

from api.agents import dispatch
from api.agents.repository import AgentSettings, add_consent, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.contacts.repository import set_contact_timezone
from api.scheduling.repository import create_event
from tests.integration.test_email_inbound_integration import _JWT, _claims, _contact, _tenant
from tests.integration.test_email_inbound_integration import env as _a3_env  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(_a3_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    db, a = _a3_env["db"], _claims(_a3_env["a"])
    await _tenant(db, a, mode="auto_send")
    await upsert_agent_settings(db, a, AgentSettings(
        enabled_channels=("call", "email"), physical_address="1 Main St",
        min_hours_between_touches=1))
    return _a3_env


def _cookie(tenant: str, role: Role = Role.CLIENT_ADMIN) -> dict[str, str]:
    token, _ = create_access_token(AuthClaims(subject="u", role=role, tenant_id=tenant),
                                   secret=_JWT, ttl_seconds=300)
    return {"access_token": token}


async def _missed(env: dict[str, Any], *, calls_ok: bool = True) -> tuple[str, str]:
    """A contact who booked a call that has already started. Returns (contact, event)."""
    db, a = env["db"], _claims(env["a"])
    contact_id = await _contact(db, a, email="jane@example.com")
    await set_contact_timezone(db, a, contact_id, "America/New_York")
    if calls_ok:
        await add_consent(db, a, contact_id, channel="call", status="granted",
                          basis="express_written_consent", source="form")
    past = datetime.now(UTC) - timedelta(hours=2)
    event = await create_event(db, a, starts_at=past, ends_at=past + timedelta(minutes=30),
                               timezone="America/New_York", visitor_id=None, lead_id=None,
                               consent={}, email="jane@example.com", name="Jane")
    return contact_id, event.event_id


async def _mark(env: dict[str, Any], event_id: str, outcome: str) -> None:
    r = await env["client"].post(f"/admin/outreach/meetings/{event_id}:outcome",
                                 json={"outcome": outcome}, cookies=_cookie(env["a"]))
    assert r.status_code == 200, r.text
    await dispatch.dispatch_events(env["db"], limit=500)


async def _plans(env: dict[str, Any], contact_id: str) -> list[Any]:
    return list(await env["db"].fetch(
        "SELECT plan_id, plan->>'kind' AS kind, status, stop_reason FROM outreach_plans "
        "WHERE contact_id = $1 "
        "ORDER BY created_at", contact_id))


async def _steps(env: dict[str, Any], plan_id: str) -> list[tuple[str, str]]:
    return [(r["channel"], r["status"]) for r in await env["db"].fetch(
        "SELECT channel, status FROM outreach_tasks WHERE plan_id = $1 ORDER BY step_index",
        plan_id)]


async def test_a_no_show_gets_an_email_a_call_and_a_final_email(env: dict[str, Any]) -> None:
    contact_id, event_id = await _missed(env)
    await _mark(env, event_id, "no_show")
    [plan] = await _plans(env, contact_id)
    assert (plan["kind"], plan["status"]) == ("no_show_recovery", "active")
    assert await _steps(env, plan["plan_id"]) == [
        ("email", "scheduled"), ("call", "scheduled"), ("email", "scheduled")]
    first = await env["db"].fetchval("SELECT scheduled_at FROM outreach_tasks WHERE plan_id = $1 "
                                     "AND step_index = 0", plan["plan_id"])
    assert first <= datetime.now(UTC) + timedelta(minutes=1)  # the reschedule email, now


async def test_without_call_consent_it_is_the_two_emails_only(env: dict[str, Any]) -> None:
    contact_id, event_id = await _missed(env, calls_ok=False)
    await _mark(env, event_id, "no_show")
    [plan] = await _plans(env, contact_id)
    assert await _steps(env, plan["plan_id"]) == [("email", "scheduled"), ("email", "scheduled")]


async def test_marked_attended_after_all_stops_the_recovery(env: dict[str, Any]) -> None:
    contact_id, event_id = await _missed(env)
    await _mark(env, event_id, "no_show")
    await _mark(env, event_id, "attended")  # a correction: they did show
    plans = await _plans(env, contact_id)
    assert (plans[0]["status"], plans[0]["stop_reason"]) == ("stopped", "meeting_attended")


async def test_marking_twice_starts_one_sequence_and_other_tenants_see_nothing(
        env: dict[str, Any]) -> None:
    contact_id, event_id = await _missed(env)
    await _mark(env, event_id, "no_show")
    await _mark(env, event_id, "no_show")
    assert len(await _plans(env, contact_id)) == 1
    assert await env["db"].fetchval("SELECT count(*) FROM outreach_plans WHERE tenant_id = $1",
                                    env["b"]) == 0


async def test_a_meeting_with_nobody_we_know_starts_nothing(env: dict[str, Any]) -> None:
    db, a = env["db"], _claims(env["a"])
    past = datetime.now(UTC) - timedelta(hours=2)
    event = await create_event(db, a, starts_at=past, ends_at=past + timedelta(minutes=30),
                               timezone="America/New_York", visitor_id=None, lead_id=None,
                               consent={}, email="stranger@example.com", name="S")
    await _mark(env, event.event_id, "no_show")
    assert await db.fetchval("SELECT last_error FROM agent_events WHERE kind = 'meeting_no_show' "
                             "AND tenant_id = $1", env["a"]) == "NO_CONTACT"


# -- After the meeting (plan 2.2) ----------------------------------------------------------------


async def _attended(env: dict[str, Any], *, owner: str | None = None,
                    lead_agent: str | None = None) -> tuple[str, str]:
    db, a = env["db"], _claims(env["a"])
    contact_id = await _contact(db, a, email="jane@example.com", owner=owner)
    if lead_agent:
        from api.leads.repository import create_lead

        lead_id = await create_lead(db, a, visitor_id="v-1", name="Jane Doe",
                                    email="jane@example.com", phone=None, consent={},
                                    source="widget")
        await db.execute("UPDATE leads SET assigned_agent_id = $2 WHERE lead_id = $1", lead_id,
                         lead_agent)
        await db.execute("UPDATE contacts SET lead_id = $2 WHERE contact_id = $1", contact_id,
                         lead_id)
    past = datetime.now(UTC) - timedelta(hours=2)
    event = await create_event(db, a, starts_at=past, ends_at=past + timedelta(minutes=30),
                               timezone="America/New_York", visitor_id=None, lead_id=None,
                               consent={}, email="jane@example.com", name="Jane Doe")
    return contact_id, event.event_id


async def _opportunities(env: dict[str, Any], contact_id: str) -> list[Any]:
    return list(await env["db"].fetch(
        "SELECT name, stage, currency, owner_agent_id FROM opportunities WHERE contact_id = $1",
        contact_id))


async def test_attended_creates_an_opportunity_owned_by_the_leads_agent(
        env: dict[str, Any]) -> None:
    contact_id, event_id = await _attended(env, lead_agent="agent-7")
    await _mark(env, event_id, "attended")
    [opp] = await _opportunities(env, contact_id)
    assert (opp["stage"], opp["owner_agent_id"]) == ("prospecting", "agent-7")
    assert opp["name"] == "Jane Doe" and opp["currency"]


async def test_without_a_lead_agent_the_contacts_owner_takes_it(env: dict[str, Any]) -> None:
    contact_id, event_id = await _attended(env, owner="agent-3")
    await _mark(env, event_id, "attended")
    [opp] = await _opportunities(env, contact_id)
    assert opp["owner_agent_id"] == "agent-3"


async def test_attended_drafts_a_thank_you_for_a_person_to_review(env: dict[str, Any]) -> None:
    """The AI wasn't in the meeting: the thank-you is ALWAYS a draft, even for a client whose
    emails otherwise send automatically."""
    db = env["db"]
    contact_id, event_id = await _attended(env)
    await _mark(env, event_id, "attended")
    [plan] = await _plans(env, contact_id)
    assert (plan["kind"], plan["status"]) == ("after_meeting", "active")
    goal = await db.fetchval("SELECT goal FROM outreach_tasks WHERE plan_id = $1",
                             plan["plan_id"])
    assert goal == "thank_you"
    sent_before = len(env["mailer"].sent)
    await dispatch.dispatch_due_tasks(db, limit=10)
    assert await db.fetchval("SELECT m.status FROM email_messages m JOIN email_threads t "
                             "USING (tenant_id, thread_id) WHERE t.contact_id = $1",
                             contact_id) == "draft"
    assert len(env["mailer"].sent) == sent_before  # nothing went out


async def test_attended_twice_makes_one_opportunity_and_one_draft(env: dict[str, Any]) -> None:
    contact_id, event_id = await _attended(env)
    await _mark(env, event_id, "attended")
    await _mark(env, event_id, "attended")
    assert len(await _opportunities(env, contact_id)) == 1
    assert len(await _plans(env, contact_id)) == 1


async def test_a_no_show_corrected_to_attended_gets_the_after_meeting_steps(
        env: dict[str, Any]) -> None:
    contact_id, event_id = await _missed(env)
    await _mark(env, event_id, "no_show")
    await _mark(env, event_id, "attended")
    kinds = [(p["kind"], p["status"]) for p in await _plans(env, contact_id)]
    assert kinds == [("no_show_recovery", "stopped"), ("after_meeting", "active")]
    assert len(await _opportunities(env, contact_id)) == 1
    assert await env["db"].fetchval("SELECT count(*) FROM opportunities WHERE tenant_id = $1",
                                    env["b"]) == 0
