"""A14: the final scope's acceptance criteria (§14) end to end, on a REAL migrated
Postgres, through production's entry points (intake webhooks, widget, admin API,
Inbound Parse, signed Plivo webhooks, the dispatchers). Uses the A9 E2E fixture:
model, SendGrid and Plivo's REST API are faked; everything else is real.
See specs/sprints/A14.md for the criterion -> test map."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from uuid import uuid4

import pytest

from api.agents.consent_wording import CURRENT_VERSION
from api.agents.repository import AgentSettings, add_suppression, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.email_agent.links import sign_route
from api.gateway.sessions import mint_visitor_session
from api.intake.routes import sign_intake
from tests.integration.test_e2e_journeys_integration import _JWT, _NY, _open_zone, _settle
from tests.integration.test_e2e_journeys_integration import env as _e2e_env  # noqa: F401

pytestmark = pytest.mark.integration

_ALL_DAY = {"email_window_start": dtime(0), "email_window_end": dtime(23, 59)}


@pytest.fixture
def env(_e2e_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    return _e2e_env


async def _settings(env: dict[str, Any], **over: Any) -> None:
    values: dict[str, Any] = {"enabled_channels": ("email",), "email_mode": "auto_send",
                              "physical_address": "1 Main St, Miami FL",
                              "inbound_auto_enabled": True,
                              "first_inbound_email_delay_minutes": 0, **_ALL_DAY, **over}
    await upsert_agent_settings(env["db"], env["claims"], AgentSettings(**values))


async def _widget_lead(env: dict[str, Any], email: str, *, call: bool = False,
                       phone: str | None = None) -> None:
    visitor, _ = mint_visitor_session(env["tenant"], secret=_JWT, ttl_seconds=600)
    r = await env["client"].post(
        "/public/leads", headers={"Authorization": f"Bearer {visitor}"}, json={
            "name": "Jane Doe", "email": email, "phone": phone,
            "consent": {"granted": True, "purpose": "lead_followup", "text": "I agree."},
            "channel_consents": {"email": True, "call": call, "version": CURRENT_VERSION},
            "timezone": _open_zone()})
    assert r.status_code == 201, r.text


async def _reply(env: dict[str, Any], to_email: Any, body: str, *,
                 sender: str = "jane@example.com") -> None:
    raw = "\r\n".join([
        f"From: Jane Doe <{sender}>", f"To: {to_email.reply_to}", "Subject: Re: hello",
        f"Message-ID: <{uuid4().hex}@example.com>",
        f"In-Reply-To: {to_email.message_id}", f"References: {to_email.message_id}",
        "Content-Type: text/plain; charset=utf-8", "", body, ""])
    r = await env["client"].post(f"/public/email/inbound/{sign_route(env['tenant'])}", data={
        "email": raw, "SPF": "pass", "dkim": "{@example.com : pass}"})
    assert r.status_code == 200, r.text
    await _settle(env)


async def _active_plans(env: dict[str, Any]) -> int:
    return int(await env["db"].fetchval(
        "SELECT count(*) FROM outreach_plans WHERE tenant_id = $1 AND status = 'active'",
        env["tenant"]))


async def test_14_1_meta_notification_becomes_a_lead_and_gets_followed_up(
    env: dict[str, Any],
) -> None:
    db, tid = env["db"], env["tenant"]
    await _settings(env)
    raw = "\r\n".join([
        "From: Facebook <notification@facebookmail.com>", "To: rebecca@acme.com",
        "Subject: New lead: Spring check-ups", f"Message-ID: <{uuid4().hex}@facebookmail.com>",
        "Content-Type: text/plain; charset=utf-8", "",
        "You have a new lead!\n\nFull name: Jane Doe\nEmail: jane@example.com\n"
        "What service do you need?: Flat roof repair\n", ""])
    url = f"/public/intake/email/{sign_intake(tid, 'email')}"
    form = {"email": raw, "SPF": "pass", "dkim": "{@facebookmail.com : pass}"}
    first = await env["client"].post(url, data=form)
    again = await env["client"].post(url, data=form)  # Gmail forwarded it twice
    assert (first.json()["status"], again.json()["status"]) == ("accepted", "duplicate")
    await _settle(env, now=datetime.now(UTC) + timedelta(minutes=1))

    lead = await db.fetchrow("SELECT source, email FROM leads WHERE tenant_id = $1", tid)
    assert lead is not None and tuple(lead) == ("meta_email", "jane@example.com")
    consent = await db.fetchrow("SELECT channel, source FROM contact_consents "
                                "WHERE tenant_id = $1", tid)
    assert consent is not None and tuple(consent) == ("email", "meta")
    assert [e.to for e in env["mailer"].sent] == ["jane@example.com"]  # nobody forwarded it


async def test_14_2_import_skips_suppressed_and_duplicate_rows(env: dict[str, Any]) -> None:
    db, client, tid = env["db"], env["client"], env["tenant"]
    await _settings(env, inbound_auto_enabled=False)
    await add_suppression(db, env["claims"], channel="email", value="gone@example.com",
                          reason="unsubscribe", source="test")
    admin, _ = create_access_token(env["claims"], secret=_JWT, ttl_seconds=600)
    csv = ("name,email\nAna,ana@example.com\nGone,gone@example.com\n"
           "Ana again,ANA@example.com\nBo,bo@example.com\n")
    r = await client.post("/admin/outreach/imports", cookies={"access_token": admin},
                          files={"file": ("list.csv", csv.encode(), "text/csv")},
                          data={"campaign_name": "List"})
    report = r.json()["report"]
    assert (report["valid"], report["rejected_count"]) == (2, 2)
    assert {tuple(x["reasons"]) for x in report["rejected"]} == {
        ("SUPPRESSED",), ("DUPLICATE_IN_FILE",)}
    await client.post(f"/admin/outreach/imports/{r.json()['import_id']}:commit",
                      cookies={"access_token": admin}, json={})
    await _settle(env)
    assert sorted(e.to for e in env["mailer"].sent) == ["ana@example.com", "bo@example.com"]
    assert await db.fetchval("SELECT count(*) FROM contacts WHERE tenant_id = $1", tid) == 2


async def test_14_3_multi_turn_replies_are_answered_in_context(env: dict[str, Any]) -> None:
    mailer, model = env["mailer"], env["model"]
    await _settings(env)
    await _widget_lead(env, "jane@example.com")
    await _settle(env, now=datetime.now(UTC) + timedelta(minutes=1))
    [first] = mailer.sent
    model.label = "question"
    for question in ("Do you fix flat roofs?", "How long does an inspection take?",
                     "And do you do gutters as well?"):
        await _reply(env, first, question)
    answers = [e for e in mailer.sent[1:] if e.to == "jane@example.com"]
    assert len(answers) == 3  # every turn answered -- no fixed sequence ends the talk
    last_prompt = model.prompts[-1][-1].content
    assert "Do you fix flat roofs?" in last_prompt  # earlier turns travel with the latest
    assert await _active_plans(env) == 1  # exactly one follow-up waiting for silence


async def test_14_4_not_interested_stops_every_channel(env: dict[str, Any]) -> None:
    db, tid, plivo = env["db"], env["tenant"], env["plivo"]
    await _settings(env, enabled_channels=("call", "email"))
    await _widget_lead(env, "jane@example.com", call=True, phone="(202) 555-0123")
    await _settle(env, now=datetime.now(UTC) + timedelta(minutes=1))
    first = env["mailer"].sent[0]
    env["model"].label = "not_interested"
    await _reply(env, first, "No thanks, we went with someone else.")
    assert await _active_plans(env) == 0
    reasons = {r["reason"] for r in await db.fetch(
        "SELECT reason FROM suppression_list WHERE tenant_id = $1", tid)}
    assert reasons == {"not_interested"}
    await _settle(env, now=datetime.now(UTC) + timedelta(days=5))
    assert "place_call" not in plivo.names  # the call step never happens


async def test_14_5_a_form_phone_number_alone_never_gets_a_call(env: dict[str, Any]) -> None:
    db, tid, plivo = env["db"], env["tenant"], env["plivo"]
    await _settings(env, enabled_channels=("call", "email"))
    r = await env["client"].post(f"/public/intake/forms/{sign_intake(tid, 'form')}", json={
        "name": "Ana Ruiz", "email": "ana@example.com", "phone": "(202) 555-0199",
        "call_consent": "yes", "submission_id": "s-1"})  # no call wording saved by the client
    assert r.json()["status"] == "accepted"
    for days in (0, 3, 6):
        await _settle(env, now=datetime.now(UTC) + timedelta(days=days, minutes=1))
    assert "place_call" not in plivo.names
    assert await db.fetchval("SELECT count(*) FROM contact_consents WHERE tenant_id = $1 "
                             "AND channel = 'call'", tid) == 0


async def test_14_6_two_people_asking_for_the_same_slot_get_one_booking(
    env: dict[str, Any],
) -> None:
    db, tid, mailer, model = env["db"], env["tenant"], env["mailer"], env["model"]
    await _settings(env)
    await _widget_lead(env, "jane@example.com")
    await _widget_lead(env, "ana@example.com")
    await _settle(env, now=datetime.now(UTC) + timedelta(minutes=1))
    to_jane = next(e for e in mailer.sent if e.to == "jane@example.com")
    to_ana = next(e for e in mailer.sent if e.to == "ana@example.com")
    day: date = (datetime.now(_NY) + timedelta(days=2)).date()
    model.label, model.proposed = "book_meeting", f"{day.isoformat()}T15:00"
    await _reply(env, to_jane, "Can we meet at 3pm that day?")
    await _reply(env, to_ana, "Can we meet at 3pm that day?", sender="ana@example.com")
    slot = datetime(day.year, day.month, day.day, 15, tzinfo=_NY).astimezone(UTC)
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1 "
                             "AND starts_at = $2 AND status = 'booked'", tid, slot) == 1
    booked = await db.fetchval("SELECT email FROM schedule_events WHERE tenant_id = $1", tid)
    assert booked == "jane@example.com"  # first come; Ana was offered other times / a person


async def test_14_9_out_of_office_then_a_late_reply(env: dict[str, Any]) -> None:
    db, tid, mailer, model = env["db"], env["tenant"], env["mailer"], env["model"]
    await _settings(env)
    await _widget_lead(env, "jane@example.com")
    await _settle(env, now=datetime.now(UTC) + timedelta(minutes=1))
    first = mailer.sent[0]
    before = await db.fetchval(
        "SELECT min(scheduled_at) FROM outreach_tasks WHERE tenant_id = $1 "
        "AND status = 'scheduled'", tid)
    model.label = "question"
    raw_ooo = "\r\n".join([
        "From: Jane Doe <jane@example.com>", f"To: {first.reply_to}",
        "Subject: Automatic reply: hello", "Auto-Submitted: auto-replied",
        f"Message-ID: <{uuid4().hex}@example.com>", f"In-Reply-To: {first.message_id}",
        "Content-Type: text/plain; charset=utf-8", "", "I'm out of the office until Monday.", ""])
    await env["client"].post(f"/public/email/inbound/{sign_route(tid)}", data={
        "email": raw_ooo, "SPF": "pass", "dkim": "{@example.com : pass}"})
    await _settle(env)
    after = await db.fetchval(
        "SELECT min(scheduled_at) FROM outreach_tasks WHERE tenant_id = $1 "
        "AND status = 'scheduled'", tid)
    assert await _active_plans(env) == 1 and after > before  # snoozed, not stopped
    assert len(mailer.sent) == 1  # no answer to an auto-reply

    await _reply(env, first, "Back now - do you fix flat roofs?")  # days later, same thread
    assert mailer.sent[-1].to == "jane@example.com" and mailer.sent[-1].in_reply_to
    assert await _active_plans(env) == 1  # the old plan replaced by one follow-up
