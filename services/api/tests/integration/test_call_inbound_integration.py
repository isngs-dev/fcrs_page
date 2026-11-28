"""A7 inbound calls on a REAL migrated Postgres, reusing the A6 fixture (Plivo
faked, webhooks signed with the tenant's token): number routing, caller-ID,
callback scheduling, opt-out, warm transfer with whisper, unknown caller -> lead."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from common.auth import AuthClaims, Role
from common.errors import ConflictError

from api.agents import dispatch, memory
from api.call_agent import repository as repo
from api.call_agent import script, service
from api.contacts.repository import add_identity, create_contact, set_contact_timezone
from api.voice_agent.plivo import compute_signature
from tests.integration.test_call_agent_integration import _BASE, _TOKENS
from tests.integration.test_call_agent_integration import env as _a6_env  # noqa: F401

pytestmark = pytest.mark.integration

_NUMBER = "+18884810711"
_CALLER = "+12025550123"


def _claims(tenant: str) -> AuthClaims:
    return AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)


async def _inbound(env: dict[str, Any], action: str, params: dict[str, str], *,
                   token: str | None = None) -> Any:
    path = f"/public/call-agent/inbound/{action}"
    nonce = uuid4().hex
    signature = compute_signature(_BASE + path, params, nonce, token or _TOKENS["a"])
    return await env["client"].post(path, data=params, headers={
        "X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce})


async def _call_in(env: dict[str, Any], *, caller: str = _CALLER, uuid: str = "in-1") -> tuple[
        Any, str]:
    r = await _inbound(env, "answer", {"From": caller.lstrip("+"), "To": _NUMBER.lstrip("+"),
                                       "CallUUID": uuid})
    call_id = await env["db"].fetchval(
        "SELECT call_id FROM call_sessions WHERE provider_call_uuid = $1", uuid)
    return r, str(call_id)


async def _turn(env: dict[str, Any], call_id: str, speech: str) -> str:
    path = f"/public/call-agent/calls/{call_id}/turn?ticket={service.ticket(call_id)}"
    params = {"Speech": speech, "CallUUID": "in-1"}
    nonce = uuid4().hex
    r = await env["client"].post(path, data=params, headers={
        "X-Plivo-Signature-V3": compute_signature(_BASE + path, params, nonce, _TOKENS["a"]),
        "X-Plivo-Signature-V3-Nonce": nonce})
    assert r.status_code == 200, r.text
    return str(r.text)


async def _known_jane(env: dict[str, Any]) -> str:
    db, claims = env["db"], _claims(env["a"])
    cid = await create_contact(db, claims, account_id=None, lead_id=None, name="Jane Doe",
                               email="jane@example.com", phone=_CALLER, consent={})
    await add_identity(db, claims, cid, identity_type="phone", identity_value=_CALLER)
    await set_contact_timezone(db, claims, cid, env["zone"])
    return cid


@pytest.fixture
async def routed(_a6_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    env = _a6_env
    await env["db"].execute("TRUNCATE telephony_numbers")
    await repo.register_number(env["db"], _claims(env["a"]), _NUMBER)
    return env


async def test_known_caller_is_greeted_by_name(routed: dict[str, Any]) -> None:
    await _known_jane(routed)
    r, call_id = await _call_in(routed)
    assert r.status_code == 200
    assert service.spoken_text(r.text) == script.inbound_greeting(
        first_name="Jane", company="Acme Roofing", recording=True, known=True)
    session = await repo.get_session(routed["db"], _claims(routed["a"]), call_id)
    assert session is not None and session.direction == "inbound" and session.contact_id


async def test_non_us_numbers_in_plivos_format_are_routed(routed: dict[str, Any]) -> None:
    """Plivo posts full international numbers WITHOUT '+' ("912264233234"). A live Indian
    number got 401 on every call (caller heard "busy") because only US-shaped numbers
    were understood without the '+'."""
    db, a = routed["db"], _claims(routed["a"])
    await repo.register_number(db, a, "+912264233234")
    r = await _inbound(routed, "answer", {"From": "919812345591", "To": "912264233234",
                                          "CallUUID": "in-india"})
    assert r.status_code == 200, r.text
    caller = await db.fetchval(
        "SELECT from_e164 FROM call_sessions WHERE provider_call_uuid = 'in-india'")
    assert caller == "+919812345591"
    # Same tenant check as for US numbers: B's token can't answer for A's Indian number.
    forged = await _inbound(routed, "answer", {"From": "919812345591", "To": "912264233234",
                                               "CallUUID": "x2"}, token=_TOKENS["b"])
    assert forged.status_code == 401


async def test_routing_needs_a_registered_number_and_that_tenants_signature(
    routed: dict[str, Any],
) -> None:
    db = routed["db"]
    unknown = await _inbound(routed, "answer", {"From": "12025550123", "To": "18005550000",
                                                "CallUUID": "x"})
    assert unknown.status_code == 401
    forged = await _inbound(routed, "answer", {"From": "12025550123", "To": _NUMBER.lstrip("+"),
                                               "CallUUID": "x"}, token=_TOKENS["b"])
    assert forged.status_code == 401
    with pytest.raises(ConflictError):
        await repo.register_number(db, _claims(routed["b"]), _NUMBER)
    assert await db.fetchval("SELECT count(*) FROM call_sessions WHERE direction = 'inbound'") == 0


async def test_call_me_back_records_consent_and_schedules_tomorrow(routed: dict[str, Any]) -> None:
    db = routed["db"]
    cid = await _known_jane(routed)
    _, call_id = await _call_in(routed)
    reply = await _turn(routed, call_id, "I'm busy right now, can you call me back tomorrow?")
    assert script.CALLBACK_LINE in service.spoken_text(reply)
    consent = await db.fetchrow("SELECT basis, source FROM contact_consents "
                                "WHERE contact_id = $1 AND channel = 'call'", cid)
    assert consent is not None and tuple(consent) == ("express_consent", "call")
    when = await db.fetchval("SELECT t.scheduled_at FROM outreach_tasks t JOIN outreach_plans p "
                             "USING (tenant_id, plan_id) WHERE p.contact_id = $1 "
                             "AND t.channel = 'call' AND t.status = 'scheduled'", cid)
    assert when is not None
    assert timedelta(hours=23) < when - datetime.now(UTC) < timedelta(hours=25)


async def test_unknown_caller_asking_for_a_callback_goes_to_the_team(
    routed: dict[str, Any],
) -> None:
    _, call_id = await _call_in(routed, caller="+13055550199")
    reply = await _turn(routed, call_id, "Call me back later please")
    assert script.TEAM_LINE in service.spoken_text(reply)
    assert await routed["db"].fetchval(
        "SELECT count(*) FROM notification_events WHERE kind = 'agent_call_needs_human' "
        "AND tenant_id = $1", routed["a"]) == 1


async def test_dont_call_me_suppresses_the_callers_number(routed: dict[str, Any]) -> None:
    await _known_jane(routed)
    _, call_id = await _call_in(routed)
    await _turn(routed, call_id, "Stop calling me")
    value = await routed["db"].fetchval("SELECT value_normalised FROM suppression_list "
                                        "WHERE channel = 'call'")
    assert value == _CALLER  # the caller (From), not our own number (To)


async def test_warm_transfer_whispers_who_and_what(routed: dict[str, Any]) -> None:
    db, a = routed["db"], routed["a"]
    config = await repo.get_call_config(db, _claims(a))
    assert config is not None
    values = config.__dict__ | {"transfer_number": "+13055550100"}
    await repo.upsert_call_config(db, _claims(a), values=values, auth_token=None)
    await _known_jane(routed)
    _, call_id = await _call_in(routed)
    await _turn(routed, call_id, "What's your warranty on roof repairs?")
    dial = await _turn(routed, call_id, "Can I speak to a real person")
    assert "<Dial" in dial and "<Number>+13055550100</Number>" in dial
    assert f'/public/call-agent/calls/{call_id}/whisper?ticket=' in dial

    path = f"/public/call-agent/calls/{call_id}/whisper?ticket={service.ticket(call_id)}"
    nonce = uuid4().hex
    whisper = await routed["client"].post(path, data={}, headers={
        "X-Plivo-Signature-V3": compute_signature(_BASE + path, {}, nonce, _TOKENS["a"]),
        "X-Plivo-Signature-V3-Nonce": nonce})
    said = service.spoken_text(whisper.text)
    assert said.startswith("Rebecca transferring a caller: Jane Doe.")
    assert "warranty" in said

    path = f"/public/call-agent/calls/{call_id}/dial-status?ticket={service.ticket(call_id)}"
    params = {"DialStatus": "no-answer"}
    nonce = uuid4().hex
    missed = await routed["client"].post(path, data=params, headers={
        "X-Plivo-Signature-V3": compute_signature(_BASE + path, params, nonce, _TOKENS["a"]),
        "X-Plivo-Signature-V3-Nonce": nonce})
    assert script.TRANSFER_MISSED_LINE in service.spoken_text(missed.text)
    # The whole conversation is in the transcript the console shows -- the apology too
    turns = await routed["db"].fetch("SELECT speaker, text FROM call_transcript_turns "
                                     "WHERE call_id = $1 ORDER BY turn_index", call_id)
    assert turns[-1]["speaker"] == "rebecca" and turns[-1]["text"] == script.TRANSFER_MISSED_LINE
    assert [t["speaker"] for t in turns[:3]] == ["rebecca", "caller", "rebecca"]


async def test_unknown_caller_who_engaged_becomes_a_lead(routed: dict[str, Any]) -> None:
    db, a = routed["db"], routed["a"]
    _, call_id = await _call_in(routed, caller="+13055550199", uuid="in-1")
    await _turn(routed, call_id, "Do you install metal roofs?")
    r = await _inbound(routed, "hangup", {"From": "13055550199", "To": _NUMBER.lstrip("+"),
                                          "CallUUID": "in-1", "Duration": "40",
                                          "CallStatus": "completed"})
    assert r.status_code == 200
    await dispatch.dispatch_events(db, limit=10)
    lead = await db.fetchrow("SELECT phone, source FROM leads WHERE tenant_id = $1", a)
    assert lead is not None and tuple(lead) == ("+13055550199", "inbound_call")


async def test_inbound_switched_off_hangs_up_politely(routed: dict[str, Any]) -> None:
    db, a = routed["db"], routed["a"]
    config = await repo.get_call_config(db, _claims(a))
    assert config is not None
    await repo.upsert_call_config(db, _claims(a), values=config.__dict__ | {
        "inbound_enabled": False}, auth_token=None)
    r, _ = await _call_in(routed)
    assert service.spoken_text(r.text) == script.INBOUND_OFF_LINE
    assert r.text.endswith("<Hangup/></Response>")


# -- A returning caller: the last conversation, their booking, cancel or move it -------------
# (owner, 2026-11-27)

_NY = ZoneInfo("America/New_York")


async def _booking(env: dict[str, Any], tenant: str, email: str = "jane@example.com") -> tuple[
        str, datetime]:
    from api.scheduling.repository import record_form_booking

    at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(15), tzinfo=_NY)
    event_id = await record_form_booking(
        env["db"], _claims(tenant), ref=f"form:{uuid4().hex}", starts_at=at,
        ends_at=at + timedelta(minutes=30), timezone="America/New_York", lead_id=uuid4().hex,
        email=email, name="Jane Doe", consent={"granted": True})
    assert event_id is not None
    return event_id, at


async def _status(env: dict[str, Any], event_id: str) -> str:
    return str(await env["db"].fetchval("SELECT status FROM schedule_events WHERE event_id = $1",
                                        event_id))


async def test_a_caller_with_a_booking_hears_it_and_can_cancel_it(routed: dict[str, Any]) -> None:
    db, a, b = routed["db"], routed["a"], routed["b"]
    cid = await _known_jane(routed)
    event_id, at = await _booking(routed, a)
    others, _ = await _booking(routed, b)  # tenant B's Jane: never touched
    r, call_id = await _call_in(routed)
    assert service.spoken_text(r.text) == script.inbound_greeting(
        first_name="Jane", company="Acme Roofing", recording=True, booked_at=at,
        timezone="America/New_York")

    offer = service.spoken_text(await _turn(routed, call_id, "Yes, I need to cancel my call"))
    assert offer == script.cancel_offer_line(at, "America/New_York")
    assert await _status(routed, event_id) == "booked"  # not before they say so
    done = service.spoken_text(await _turn(routed, call_id, "No, just cancel it please"))
    assert done == script.cancelled_line(at, "America/New_York")
    assert await _status(routed, event_id) == "cancelled"
    assert await _status(routed, others) == "booked"
    assert await db.fetchval("SELECT count(*) FROM audit_events WHERE tenant_id = $1 "
                             "AND action = 'scheduling.booking_cancelled'", a) == 1
    assert await db.fetchval("SELECT count(*) FROM notification_events WHERE tenant_id = $1 "
                             "AND kind = 'booking_cancelled_by_attendee'", a) == 1
    notes = [i["text"] for i in await memory.items(db, _claims(a), cid)]
    assert any(n.startswith("Cancelled their call") for n in notes)
    # The booking is gone from the call too: "goodbye" no longer names it.
    bye = service.spoken_text(await _turn(routed, call_id, "No, that's all, thanks. Bye."))
    assert "your call on" not in bye


async def test_asked_to_cancel_they_can_move_it_instead(routed: dict[str, Any]) -> None:
    await _known_jane(routed)
    event_id, at = await _booking(routed, routed["a"])
    _, call_id = await _call_in(routed)
    await _turn(routed, call_id, "I'd like to cancel that booking")
    ask = service.spoken_text(await _turn(routed, call_id, "Actually yes, let's move it"))
    assert ask == script.OTHER_TIME_LINE
    assert await _status(routed, event_id) == "booked"


async def test_yes_to_calling_about_the_booking_asks_keep_move_or_cancel(
        routed: dict[str, Any]) -> None:
    await _known_jane(routed)
    event_id, at = await _booking(routed, routed["a"])
    _, call_id = await _call_in(routed)
    what = service.spoken_text(await _turn(routed, call_id, "Yes"))
    assert what == script.INBOUND_BOOKED_WHAT_LINE
    kept = service.spoken_text(await _turn(routed, call_id, "keep it"))
    assert kept == script.kept_line(at, "America/New_York")
    assert await _status(routed, event_id) == "booked"


async def test_a_returning_caller_hears_what_they_reached_out_about(
        routed: dict[str, Any]) -> None:
    cid = await _known_jane(routed)
    await memory.remember(routed["db"], _claims(routed["a"]), cid, channel="form", kind="need",
                          text="Service: seo")
    r, _ = await _call_in(routed)
    assert service.spoken_text(r.text) == script.inbound_greeting(
        first_name="Jane", company="Acme Roofing", recording=True, about="SEO")


# -- Roof-Engine call review (2026-11-28) -----------------------------------------------------


async def test_yes_to_the_cancel_offer_cancels_and_no_call_is_offered_after(
        routed: dict[str, Any]) -> None:
    """Live 11-28: "cancel it" -> offer -> "Yeah." -> asked again, "Please confirm" -> "Yes." ->
    "What questions can I answer?"; then "No." got "our team can walk you through this"."""
    await _known_jane(routed)
    event_id, at = await _booking(routed, routed["a"])
    _, call_id = await _call_in(routed)
    await _turn(routed, call_id, "I want to cancel my call")
    done = service.spoken_text(await _turn(routed, call_id, "Yeah."))
    assert done == script.cancelled_line(at, "America/New_York")
    assert await _status(routed, event_id) == "cancelled"
    bye = await _turn(routed, call_id, "No.")
    assert service.spoken_text(bye) == script.GOODBYE_LINE  # no "walk you through this" offer
    assert bye.endswith("<Hangup/></Response>")


async def test_an_unclear_reply_to_the_cancel_offer_is_asked_once_more(
        routed: dict[str, Any]) -> None:
    await _known_jane(routed)
    event_id, at = await _booking(routed, routed["a"])
    _, call_id = await _call_in(routed)
    await _turn(routed, call_id, "cancel that call")
    again = service.spoken_text(await _turn(routed, call_id, "and I'll"))
    assert again == script.CANCEL_CLARIFY_LINE
    assert await _status(routed, event_id) == "booked"
    done = service.spoken_text(await _turn(routed, call_id, "cancel it"))
    assert done == script.cancelled_line(at, "America/New_York")


async def test_hello_mid_call_repeats_her_question(routed: dict[str, Any]) -> None:
    await _known_jane(routed)
    _, call_id = await _call_in(routed)
    asked = service.spoken_text(await _turn(routed, call_id, "I wanted to book a call"))
    again = service.spoken_text(await _turn(routed, call_id, "Hello?"))
    assert again == script.still_here_line(asked)


async def test_a_thank_you_after_an_answer_closes_the_call(routed: dict[str, Any]) -> None:
    await _known_jane(routed)
    event_id, at = await _booking(routed, routed["a"])
    _, call_id = await _call_in(routed)
    await _turn(routed, call_id, "Yes")
    await _turn(routed, call_id, "keep it")
    bye = await _turn(routed, call_id, "Yep. Thank you.")
    assert service.spoken_text(bye) == script.booked_goodbye_line(at, "America/New_York")
    assert bye.endswith("<Hangup/></Response>")
