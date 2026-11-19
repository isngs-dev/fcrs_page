"""A6 Calling Agent on a REAL migrated Postgres: dispatcher -> Plivo (faked) ->
signed webhooks -> turns -> booking / opt-out / voicemail -> hangup settles the
task -> call_completed. Plivo's API is a recording fake; the webhook requests are
signed exactly as Plivo signs them (X-Plivo-Signature-V3, the tenant's token)."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from httpx import ASGITransport, AsyncClient

from api.agents import dispatch, executors, memory, planner
from api.agents.executors import ExecutorResult
from api.agents.repository import AgentSettings, add_consent, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.call_agent import repository as repo
from api.call_agent import script, service
from api.call_agent.executor import place_call
from api.call_agent.telephony import PlacedCall, Recording
from api.config import get_api_settings
from api.contacts.repository import add_identity, create_contact, set_contact_timezone
from api.scheduling import booking
from api.scheduling.calendar import CalendarRef
from api.scheduling.repository import upsert_availability
from api.voice_agent.plivo import compute_signature

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)

_JWT = "x" * 48
_BASE = "https://api.example.com"
_REP = "rep@example.com"  # the connected calendar's owner
_NY = ZoneInfo("America/New_York")  # the business's zone: every call time is in it
_HOOK = "I saw you were asking about storm-damage repairs on our site."
_QUESTION = "What's going on with your roof at the moment?"


def _final(slot: datetime, tz: str, email: str | None) -> str:
    """The final check for the test contact, Jane Doe (no company on file)."""
    return script.final_check_line(slot, tz, email, who="Jane Doe")
_ENV = {
    "DEPLOYMENT_MODE": "saas", "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379", "JWT_SECRET": _JWT,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api", "LOG_LEVEL": "WARNING", "COOKIE_SECURE": "false",
    "PUBLIC_API_BASE_URL": _BASE,
}
_TOKENS = {"a": "token-for-tenant-a-0000", "b": "token-for-tenant-b-1111"}


def _open_zone() -> str:
    """A timezone where it's mid-day right now, so the calling window is open."""
    now = datetime.now(UTC)
    for zone in ("America/New_York", "America/Chicago", "America/Los_Angeles", "Pacific/Honolulu",
                 "Europe/London", "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney",
                 "America/Sao_Paulo", "Europe/Moscow", "Asia/Dubai", "Pacific/Auckland"):
        if 10 <= now.astimezone(ZoneInfo(zone)).hour < 18:
            return zone
    raise AssertionError("no zone in the calling window")


class _Plivo:
    """Records every Plivo API call the agent makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.recording_gate: asyncio.Event | None = None  # holds start_recording back

    async def place_call(self, **kw: Any) -> PlacedCall:
        self.calls.append(("place_call", kw))
        return PlacedCall(request_uuid="req-1")

    async def start_recording(self, **kw: Any) -> Recording:
        self.calls.append(("start_recording", kw))
        if self.recording_gate is not None:
            await self.recording_gate.wait()
        return Recording(recording_id="rec-1", url="https://plivo/rec-1.mp3")

    async def stop_recording(self, **kw: Any) -> None:
        self.calls.append(("stop_recording", kw))

    async def delete_recording(self, **kw: Any) -> None:
        self.calls.append(("delete_recording", kw))

    async def transfer_to_url(self, **kw: Any) -> None:
        self.calls.append(("transfer_to_url", kw))

    async def fetch_recording(self, **kw: Any) -> bytes:
        self.calls.append(("fetch_recording", kw))
        return b"ID3-fake-audio"

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class _Redis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    from common.settings import get_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()
    assert _TEST_DB_URL is not None
    db = await Database.connect(_TEST_DB_URL, min_size=1, max_size=5, statement_cache_size=0)
    await db.execute(
        "TRUNCATE call_sessions, tenant_call_agent_configs, agent_events, outreach_tasks, "
        "outreach_plans, outreach_campaigns, agent_tool_calls, agent_runs, contact_consents, "
        "suppression_list, agent_settings, usage_costs, audit_chain_heads, audit_events, "
        "schedule_events CASCADE")
    await db.execute("UPDATE platform_flags SET value = 'false' WHERE key = 'outreach_paused'")
    plivo = _Plivo()
    monkeypatch.setattr(service, "telephony_factory", lambda auth_id, token: plivo)
    notify = MagicMock()  # one sender for the caller's and the owner's emails
    monkeypatch.setattr(service, "send_notification", notify)
    monkeypatch.setattr(booking, "send_notification", notify)

    async def answer(db: Any, claims: Any, question: str, history: Any, *, model: Any = None,
                     about_caller: Any = None, can_book: bool = False,
                     assistant: Any = None, **_: Any) -> str | None:
        return None if "warranty" in question else "We charge two hundred dollars for that."

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    hooks: list[tuple[str | None, str | None]] = []  # (tenant, what we knew about them)

    async def lines(db: Any, claims: Any, about: str | None, *, intent: Any = None,
                    model: Any = None) -> tuple[str, str]:
        hooks.append((claims.tenant_id, about))
        return _HOOK, _QUESTION

    monkeypatch.setattr(service, "opening_lines", lines)
    executors.register("call", place_call)
    executors.register_capacity("call", service.has_capacity)

    zone = _open_zone()
    tenants: dict[str, str] = {}
    for label in ("a", "b"):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, $1, $1, true, $1)", tid)
        claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tid)
        await repo.upsert_call_config(db, claims, auth_token=_TOKENS[label], values={
            "enabled": True, "plivo_auth_id": "MAXXXXXXXXXXXXXXXXXX", "from_number": "+17865550000",
            "transfer_number": None, "company_name": "Acme Roofing", "voice": "Polly.Joanna-Neural",
            "voice_model": None, "voicemail_policy": "leave_message",
            "voicemail_message": "Hi, this is Rebecca, an AI assistant for Acme Roofing. "
                                 "Call us back at 786 555 0000.",
            "max_call_seconds": 300, "max_concurrent_calls": 1, "recording_enabled": True})
        await upsert_agent_settings(db, claims, AgentSettings(
            enabled_channels=("call",), physical_address="1 Main St"))
        await upsert_availability(db, claims, timezone="America/New_York", rules={
            "slot_minutes": 30, "buffer_minutes": 0,
            "weekly_hours": {d: [["00:00", "23:30"]] for d in
                             ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}})
        tenants[label] = tid

    from api.app import create_app

    app = create_app()
    app.state.db, app.state.redis, app.state.cache = db, _Redis(), InMemoryCache()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"db": db, "client": client, "plivo": plivo, "zone": zone, "hooks": hooks,
               **tenants}
    executors.unregister("call")
    await db.close()
    get_settings.cache_clear()
    get_api_settings.cache_clear()


async def _contact_with_call_task(env: dict[str, Any], tenant: str, *, phone: str = "2025550123"
                                  ) -> tuple[str, str]:
    db = env["db"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    cid = await create_contact(db, claims, account_id=None, lead_id=None, name="Jane Doe",
                               email="jane@example.com", phone=phone, consent={})
    await add_identity(db, claims, cid, identity_type="phone", identity_value=phone)
    await set_contact_timezone(db, claims, cid, env["zone"])
    await add_consent(db, claims, cid, channel="call", status="granted",
                      basis="express_written_consent", source="csv")
    await planner.create_plan(db, claims, contact_id=cid, kind="outbound_manual", goal=None,
                              now=datetime.now(UTC) - timedelta(days=3))
    task_id = await db.fetchval("SELECT task_id FROM outreach_tasks t JOIN outreach_plans p "
                                "USING (tenant_id, plan_id) WHERE p.contact_id = $1", cid)
    return cid, str(task_id)


async def _no_questions(env: dict[str, Any], tenant: str) -> None:
    """A client with no qualification questions (plan 1.2): the moments they'd use keep their
    own lines -- the tests of those lines run this way."""
    from api.qualification import repository as qrepo
    from api.qualification.scoring import QualConfig

    await qrepo.save_config(env["db"], AuthClaims(subject="admin", role=Role.CLIENT_ADMIN,
                                                  tenant_id=tenant), QualConfig(questions=[]))


async def _dial(env: dict[str, Any], tenant: str) -> tuple[str, str]:
    cid, task_id = await _contact_with_call_task(env, tenant)
    counts = await dispatch.dispatch_due_tasks(env["db"], limit=10)
    assert counts.get("pending") == 1, counts
    call_id = await env["db"].fetchval("SELECT call_id FROM call_sessions WHERE task_id = $1",
                                       task_id)
    return str(call_id), task_id


async def _webhook(env: dict[str, Any], call_id: str, action: str, params: dict[str, str], *,
                   token: str | None = None, ticket: str | None = None) -> Any:
    path = f"/public/call-agent/calls/{call_id}/{action}?ticket={ticket or service.ticket(call_id)}"
    nonce = uuid4().hex
    signature = compute_signature(_BASE + path, params, nonce, token or _TOKENS["a"])
    return await env["client"].post(path, data=params, headers={
        "X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce})


async def _say(env: dict[str, Any], call_id: str, speech: str) -> str:
    r = await _webhook(env, call_id, "turn", {"Speech": speech, "CallUUID": "uuid-1"})
    assert r.status_code == 200, r.text
    return str(r.text)


async def _recording_saved(db: Any, call_id: str) -> None:
    for _ in range(200):
        if await db.fetchval("SELECT recording_id FROM call_sessions WHERE call_id = $1",
                             call_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the recording never started")


async def test_the_first_answer_never_waits_for_the_recording_to_start(
        env: dict[str, Any]) -> None:
    """2026-11-19 (owner: no gap at the start): starting the recording is a request to the
    phone provider; the first answer goes out without waiting for it, and the recording
    still starts and is kept on the call."""
    db, plivo, a = env["db"], env["plivo"], env["a"]
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    plivo.recording_gate = asyncio.Event()  # the provider is slow to start it
    reply = await asyncio.wait_for(_say(env, call_id, "Sure, how much is a repair?"), 5)
    assert "two hundred dollars" in reply
    assert await db.fetchval("SELECT recording_id FROM call_sessions WHERE call_id = $1",
                             call_id) is None
    plivo.recording_gate.set()
    await _recording_saved(db, call_id)


async def test_full_call_disclosure_recording_booking_and_settlement(env: dict[str, Any]) -> None:
    db, plivo, a = env["db"], env["plivo"], env["a"]
    call_id, task_id = await _dial(env, a)
    name, placed = plivo.calls[0]
    assert name == "place_call" and placed["to_e164"] == "+12025550123"
    assert urlsplit(placed["answer_url"]).path == f"/public/call-agent/calls/{call_id}/answer"
    assert placed["machine_url"].endswith(f"ticket={service.ticket(call_id)}")
    assert await db.fetchval("SELECT status FROM outreach_tasks WHERE task_id = $1",
                             task_id) == "pending"

    answer = await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert answer.status_code == 200
    first_speak = service.spoken_text(answer.text)
    assert first_speak == script.opening(first_name="Jane", company="Acme Roofing",
                                         recording=True, hook=_HOOK)
    assert env["hooks"] == [(a, None)]  # written for tenant A's contact, before dialling
    assert "start_recording" not in plivo.names()  # never before the disclosure was heard

    reply = await _say(env, call_id, "Sure, how much is a repair?")
    assert "two hundred dollars" in reply
    await _recording_saved(db, call_id)  # started alongside the answer, not before it
    assert plivo.names().count("start_recording") == 1

    await _say(env, call_id, "Please don't record this.")
    assert plivo.names()[-2:] == ["stop_recording", "delete_recording"]
    assert await db.fetchval("SELECT recording_consent FROM call_sessions WHERE call_id = $1",
                             call_id) is False

    ask = service.spoken_text(await _say(env, call_id, "I'd like to book a time"))
    assert ask == script.ASK_TIME_LINE
    confirm = service.spoken_text(await _say(env, call_id, "whatever is earliest"))
    assert confirm.startswith("Just to confirm") and script.say_email("jane@example.com") in confirm
    booked = service.spoken_text(await _say(env, call_id, "Yes please"))
    assert booked.startswith("You're booked for")
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1", a) == 1
    assert await db.fetchval("SELECT count(*) FROM reminder_jobs r JOIN schedule_events e "
                             "USING (tenant_id, event_id) WHERE e.tenant_id = $1", a) == 3

    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "Duration": "95",
                                            "CallStatus": "completed"})
    session = await db.fetchrow("SELECT status, outcome, duration_s FROM call_sessions "
                                "WHERE call_id = $1", call_id)
    assert session is not None and session["status"] == "completed"
    assert session["outcome"] == "booked" and session["duration_s"] == 95
    assert await db.fetchval("SELECT status FROM outreach_tasks WHERE task_id = $1",
                             task_id) == "completed"
    turns = await db.fetch("SELECT speaker, latency_ms FROM call_transcript_turns "
                           "WHERE call_id = $1 ORDER BY turn_index", call_id)
    assert turns[0]["speaker"] == "rebecca"  # the disclosure is the first turn
    assert all(t["latency_ms"] is not None for t in turns[1:] if t["speaker"] == "rebecca")
    await dispatch.dispatch_events(db, limit=20)
    kinds = {r["kind"]: r["last_error"] for r in await db.fetch(
        "SELECT kind, last_error FROM agent_events WHERE tenant_id = $1", a)}
    assert kinds["call_completed"] == "outcome:booked"
    assert "meeting_booked" in kinds
    assert await db.fetchval("SELECT status FROM outreach_plans WHERE tenant_id = $1",
                             a) == "stopped"  # booked -> follow-ups stop


async def test_a_failed_opening_hook_still_calls_with_the_general_line(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(service, "opening_lines", down)
    call_id, _ = await _dial(env, env["a"])
    answer = await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert service.spoken_text(answer.text) == script.opening(
        first_name="Jane", company="Acme Roofing", recording=True, hook=None)
    # no follow-up question either: "yes" gets the usual reply
    assert "What questions can I answer" in service.spoken_text(await _say(env, call_id, "yes"))


async def test_yes_to_the_opening_gets_a_question_about_what_they_need(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    await _no_questions(env, a)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert service.spoken_text(await _say(env, call_id, "yes sure")) == f"Great. {_QUESTION}"
    thanks = service.spoken_text(await _say(env, call_id, "the roof leaks over the kitchen"))
    assert thanks == script.discovery_thanks_line("Jane")
    contact_id = await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                   call_id)
    remembered = await memory.items(db, claims, str(contact_id))  # the team sees what they said
    assert remembered[-1]["text"] == "the roof leaks over the kitchen"
    ask = service.spoken_text(await _say(env, call_id, "yes"))
    assert ask == script.ASK_TIME_LINE
    # asked once only: a later "yes" never repeats the question
    assert _QUESTION not in ask


async def _answered(env: dict[str, Any], tenant: str) -> str:
    call_id, _ = await _dial(env, tenant)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "Sure, how much is a repair?")
    return call_id


async def test_caller_names_the_time_and_the_email_the_meet_link_goes_to(
        env: dict[str, Any]) -> None:
    """Live 2026-10-28: asked to schedule, Rebecca only said 'our team will send a link'.
    Now she asks for the day and time, confirms where the link goes, and books it with a
    Meet link from the connected calendar (stub provider here)."""
    from api.scheduling.calendar_config_repository import upsert_calendar_config

    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    await upsert_calendar_config(db, claims, provider="stub", calendar_id=None,
                                 credentials=uuid4().hex, busy=[], enabled=True)
    call_id = await _answered(env, a)

    ask = service.spoken_text(await _say(env, call_id, "Can you schedule a meeting with the team"))
    assert ask == script.ASK_TIME_LINE
    tomorrow = (datetime.now(_NY) + timedelta(days=1)).date()
    three = datetime.combine(tomorrow, dtime(15), tzinfo=_NY)
    confirm = service.spoken_text(await _say(env, call_id, "tomorrow at 3 pm"))
    assert confirm == _final(three, "America/New_York", "jane@example.com")
    assert service.spoken_text(await _say(env, call_id, "no, the email")) == script.EMAIL_ASK_LINE
    heard = service.spoken_text(await _say(env, call_id, "it's jane dot doe at the rate gmail "
                                                         "dot com"))
    assert heard == _final(three, "America/New_York", "jane.doe@gmail.com")
    booked = service.spoken_text(await _say(env, call_id, "yes"))
    assert booked.startswith("You're booked for") and "meeting link" in booked

    event = await db.fetchrow("SELECT event_id, email, timezone, meet_url, calendar_ref "
                              "FROM schedule_events WHERE tenant_id = $1", a)
    assert event is not None and event["email"] == "jane.doe@gmail.com"
    assert event["timezone"] == "America/New_York"
    assert event["meet_url"] == f"https://meet.google.com/stub-{event['event_id']}"
    job = await db.fetchrow("SELECT recipient, body FROM notification_jobs WHERE tenant_id = $1 "
                            "AND dedupe_key = $2", a, f"booking_confirm:{event['event_id']}")
    assert job is not None and job["recipient"] == "jane.doe@gmail.com"
    assert event["meet_url"] in job["body"]
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) == "booked"


async def test_the_callers_own_time_is_booked_with_one_confirmation(env: dict[str, Any]) -> None:
    """The live call of 2026-10-30, replayed: asked for the 3rd at 6:30 PM EST, Rebecca lost it
    all, pushed the earliest slot in India time, then took four more questions to book."""
    db, a = env["db"], env["a"]
    call_id = await _answered(env, a)
    day = (datetime.now(_NY) + timedelta(days=4)).date()

    ask = service.spoken_text(await _say(env, call_id, "yes schedule a book with the sales"))
    assert ask == script.ASK_TIME_LINE
    time_ask = service.spoken_text(await _say(env, call_id, f"can you do it on {day:%B} {day.day}"))
    assert time_ask == script.ask_hour_line(day)
    final = service.spoken_text(await _say(env, call_id, "uh 6:30 p.m. EST"))
    slot = datetime.combine(day, dtime(18, 30), tzinfo=_NY)
    assert final == _final(slot, "America/New_York", "jane@example.com")

    end = await _say(env, call_id, "yes")
    assert service.spoken_text(end) == script.booked_line(slot, "America/New_York")
    # Owner, 2026-11-14: booked, then she invites their questions instead of hanging up.
    assert not end.endswith("<Hangup/></Response>")
    event = await db.fetchrow("SELECT starts_at, timezone, email FROM schedule_events "
                              "WHERE tenant_id = $1", a)
    assert event is not None and event["starts_at"] == slot
    assert (event["timezone"], event["email"]) == ("America/New_York", "jane@example.com")
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) == "booked"
    # "Okay." after the booking never books again; "no" closes, naming the booked call.
    assert service.spoken_text(await _say(env, call_id, "Okay.")) != script.TAKEN_LINE
    bye = await _say(env, call_id, "no")
    assert service.spoken_text(bye) == script.booked_goodbye_line(slot, "America/New_York")
    assert bye.endswith("<Hangup/></Response>")
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1", a) == 1
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) == "booked"


async def test_without_a_booking_she_offers_a_call_once_before_goodbye(
        env: dict[str, Any]) -> None:
    """Owner, 2026-11-14: not booked -> a polite offer to book before the call ends, once."""
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "How much is a repair?")
    offer = await _say(env, call_id, "that's all, bye")
    assert service.spoken_text(offer) == script.CLOSE_OFFER_LINE
    assert not offer.endswith("<Hangup/></Response>")
    end = await _say(env, call_id, "no, bye")
    assert service.spoken_text(end) == script.GOODBYE_LINE
    assert end.endswith("<Hangup/></Response>")


async def test_a_date_with_the_time_right_after_it_is_heard_correctly(
        env: dict[str, Any]) -> None:
    """Live 2026-11: "4th November 6:30 p.m. EST" was read as November 6 with no time, so
    Rebecca kept asking what time on the wrong day."""
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "can I schedule the meet with your team")
    day = (datetime.now(_NY) + timedelta(days=2)).date()
    said = service.spoken_text(await _say(env, call_id, f"uh Port {day:%B} 6:30 p.m. EST"))
    assert said == script.ASK_DAY_LINE  # "Port" isn't a date, but the time is kept
    final = service.spoken_text(await _say(
        env, call_id, f"no I want to book a call on {day.day}th {day:%B} 6:30 p.m. EST"))
    assert final == _final(datetime.combine(day, dtime(18, 30), tzinfo=_NY),
                                            "America/New_York", "jane@example.com")


async def test_a_time_she_cant_make_out_doesnt_loop_forever(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    assert service.spoken_text(await _say(env, call_id, "tomorrow")) == script.ask_hour_line(day)
    again = service.spoken_text(await _say(env, call_id, "30 p.m. EST"))
    assert again == f"{script.TIME_AGAIN} {script.ask_hour_line(day)}"
    await _say(env, call_id, "30 p.m. EST")
    end = await _say(env, call_id, "30 p.m. EST")  # the team takes over
    assert service.spoken_text(end) == script.TEAM_LINE and "<Hangup" in end
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1", a) == 0


async def test_a_time_given_with_a_yes_is_kept(env: dict[str, Any]) -> None:
    """Live: "yes 6:30 p.m. EST" to "find a time?" counted as a bare yes and the time was lost."""
    call_id = await _answered(env, env["a"])
    assert "find a time" in service.spoken_text(await _say(env, call_id, "what's the warranty"))
    assert service.spoken_text(await _say(env, call_id, "yes 6:30 p.m. EST")) == script.ASK_DAY_LINE
    day = (datetime.now(_NY) + timedelta(days=2)).date()
    final = service.spoken_text(await _say(env, call_id, f"{day:%B} {day.day}"))
    assert final == _final(datetime.combine(day, dtime(18, 30), tzinfo=_NY),
                                            "America/New_York", "jane@example.com")


async def test_times_are_in_the_fixed_zone_not_the_contacts_browser_zone(
        env: dict[str, Any]) -> None:
    """US product: no zone is asked for, and an Indian test browser doesn't make it India time."""
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id = await _answered(env, a)
    contact_id = await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                   call_id)
    await set_contact_timezone(db, claims, str(contact_id), "Asia/Kolkata")
    await _say(env, call_id, "I'd like to book a time")
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    final = service.spoken_text(await _say(env, call_id, "tomorrow at 3 pm"))
    assert final == _final(datetime.combine(day, dtime(15), tzinfo=_NY),
                                            "America/New_York", "jane@example.com")
    assert "Eastern time" in final and "India" not in final


async def test_a_zone_the_caller_names_is_honoured(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    zone = ZoneInfo("America/Los_Angeles")
    day = (datetime.now(zone) + timedelta(days=1)).date()
    slot = datetime.combine(day, dtime(15), tzinfo=zone)
    final = service.spoken_text(await _say(env, call_id, "tomorrow at 3 pm Pacific"))
    assert final == _final(slot, "America/Los_Angeles", "jane@example.com")
    await _say(env, call_id, "yes")
    assert await db.fetchval("SELECT starts_at FROM schedule_events WHERE tenant_id = $1", a) == slot


async def test_a_taken_time_is_declined_and_nothing_is_proposed(env: dict[str, Any]) -> None:
    db, a, b = env["db"], env["a"], env["b"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    tomorrow = (datetime.now(_NY) + timedelta(days=1)).date()
    three = datetime.combine(tomorrow, dtime(15), tzinfo=_NY)
    from api.scheduling.repository import create_event

    await create_event(db, claims, starts_at=three, ends_at=three + timedelta(minutes=30),
                       timezone="America/New_York", visitor_id=None, lead_id=None, email=None,
                       name=None, consent={"granted": True})
    # Another tenant's meeting at the next time asked for must not count as taken here.
    await create_event(db, AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=b),
                       starts_at=three + timedelta(hours=1), ends_at=three + timedelta(minutes=90),
                       timezone="America/New_York", visitor_id=None, lead_id=None, email=None,
                       name=None, consent={"granted": True})
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    assert service.spoken_text(await _say(env, call_id, "tomorrow at 3 pm")) == script.TAKEN_LINE
    # The day is kept: they only need to give another time.
    final = service.spoken_text(await _say(env, call_id, "4 pm"))
    assert final == _final(three + timedelta(hours=1), "America/New_York",
                                            "jane@example.com")


async def test_a_time_outside_business_hours_is_not_called_booked(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    await upsert_availability(db, claims, timezone="America/New_York", rules={
        "slot_minutes": 30, "buffer_minutes": 0,
        "weekly_hours": {d: [["09:00", "17:00"]] for d in
                         ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}})
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    declined = service.spoken_text(await _say(env, call_id, "tomorrow at 6:30 pm EST"))
    assert declined == script.not_open_line([["09:00", "17:00"]], "America/New_York")
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1", a) == 0


async def test_a_closed_day_is_said_before_asking_for_a_time(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    tomorrow = (datetime.now(_NY) + timedelta(days=1)).date()
    open_days = [d for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                 if d != f"{tomorrow:%a}".lower()]
    await upsert_availability(db, claims, timezone="America/New_York", rules={
        "slot_minutes": 30, "buffer_minutes": 0,
        "weekly_hours": {d: [["09:00", "17:00"]] for d in open_days}})
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    assert service.spoken_text(await _say(env, call_id, "tomorrow")) == script.DAY_FULL


async def test_a_day_weeks_away_can_be_booked(env: dict[str, Any]) -> None:
    """Rebecca only looked 7 days ahead, so any later day sounded closed."""
    a = env["a"]
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    day = (datetime.now(_NY) + timedelta(days=20)).date()
    final = service.spoken_text(await _say(env, call_id, f"{day:%B} {day.day} at 3 pm"))
    slot = datetime.combine(day, dtime(15), tzinfo=_NY)
    assert final == _final(slot, "America/New_York", "jane@example.com")
    assert service.spoken_text(await _say(env, call_id, "yes")).startswith("You're booked for")


async def test_the_earliest_time_is_given_only_when_they_ask_for_it(env: dict[str, Any]) -> None:
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "I'd like to book a time")
    final = service.spoken_text(await _say(env, call_id, "whatever is earliest"))
    assert final.startswith("Just to confirm") and final.endswith(
        "Is that the right email, or would you like me to use a different one?")


async def test_asking_if_a_time_is_available_checks_that_time(env: dict[str, Any]) -> None:
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "I'd like to book a time")
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    final = service.spoken_text(await _say(env, call_id, "is tomorrow at 3 pm available"))
    assert final == _final(datetime.combine(day, dtime(15), tzinfo=_NY),
                                            "America/New_York", "jane@example.com")


async def test_the_details_can_be_changed_at_the_final_check(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    await _say(env, call_id, "tomorrow at 3 pm")
    assert service.spoken_text(await _say(env, call_id, "no")) == script.CHANGE_LINE
    assert service.spoken_text(await _say(env, call_id, "the email")) == script.EMAIL_ASK_LINE
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    three = datetime.combine(day, dtime(15), tzinfo=_NY)
    final = service.spoken_text(await _say(env, call_id, "jane dot doe at gmail dot com"))
    assert final == _final(three, "America/New_York", "jane.doe@gmail.com")
    moved = service.spoken_text(await _say(env, call_id, "no make it 4 pm"))
    assert moved == _final(three + timedelta(hours=1), "America/New_York",
                                            "jane.doe@gmail.com")
    assert service.spoken_text(await _say(env, call_id, "the time")) == script.OTHER_TIME_LINE
    await _say(env, call_id, "tomorrow at 5 pm")
    await _say(env, call_id, "yes that's it")  # not a goodbye: a yes to the final check
    event = await db.fetchrow("SELECT starts_at, email FROM schedule_events WHERE tenant_id = $1", a)
    assert event is not None and event["email"] == "jane.doe@gmail.com"
    assert event["starts_at"] == three + timedelta(hours=2)


async def test_an_opt_out_at_the_final_check_is_never_read_as_a_yes(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    await _say(env, call_id, "tomorrow at 3 pm")
    end = await _say(env, call_id, "yes don't call me again")
    assert service.spoken_text(end) == script.OPT_OUT_LINE
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1", a) == 0
    assert await db.fetchval("SELECT count(*) FROM suppression_list WHERE tenant_id = $1", a) == 1


async def test_without_an_email_on_file_it_is_asked_before_the_final_check(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id = await _answered(env, a)
    await db.execute("UPDATE contacts SET email = NULL WHERE tenant_id = $1", a)
    await _say(env, call_id, "I'd like to book a time")
    assert service.spoken_text(await _say(env, call_id, "tomorrow at 3 pm")) == script.EMAIL_ASK_LINE
    final = service.spoken_text(await _say(env, call_id, "jane at gmail dot com"))
    assert final == _final(datetime.combine((datetime.now(_NY) + timedelta(days=1)).date(),
                                            dtime(15), tzinfo=_NY), "America/New_York",
                           "jane@gmail.com")


async def test_the_model_can_start_the_booking_and_never_asks_for_a_time_itself(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.voice_agent.agent import BOOK_A_TIME

    async def wants(*args: Any, **kwargs: Any) -> str:
        assert kwargs["can_book"] is True
        return BOOK_A_TIME

    call_id = await _answered(env, env["a"])
    monkeypatch.setattr(service, "answer_from_knowledge", wants)
    assert service.spoken_text(await _say(env, call_id, "could the sales folks ring me")) == (
        script.ASK_TIME_LINE)


async def test_a_question_mid_booking_is_answered_and_the_booking_goes_on(
        env: dict[str, Any]) -> None:
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "I'd like to book a time")
    said = service.spoken_text(await _say(env, call_id, "how much is a repair"))
    assert "two hundred dollars" in said and said.endswith(script.WHICH_TIME)


async def test_the_rest_of_yes_this_is_a_good_time_is_not_their_need(
        env: dict[str, Any]) -> None:
    """Live 2026-11-19: Flux split "Yes, this is a good time" into "Yes" + "this is a good
    time." -- the tail was saved as their need and the booking email listed it under "What
    we'll cover". It isn't a need: she asks her question again, and their real answer counts."""
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    await _no_questions(env, a)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert service.spoken_text(await _say(env, call_id, "Yes.")) == f"Great. {_QUESTION}"
    again = service.spoken_text(await _say(env, call_id, "this is a good time."))
    assert again == f"Great. {_QUESTION}"
    contact_id = str(await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                       call_id))
    assert all("good time" not in m["text"] for m in await memory.items(db, claims, contact_id))
    thanks = service.spoken_text(await _say(env, call_id, "we need more roof replacement jobs"))
    assert thanks == script.discovery_thanks_line("Jane")
    assert (await memory.items(db, claims, contact_id))[-1]["text"] == (
        "we need more roof replacement jobs")


async def test_yes_this_is_a_good_time_in_one_turn_is_a_yes(env: dict[str, Any]) -> None:
    """Six words is too long for a bare "yes" -- it still answers "Is now a good time?"."""
    await _no_questions(env, env["a"])
    call_id, _ = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    said = service.spoken_text(await _say(env, call_id, "Yes, this is a good time."))
    assert said == f"Great. {_QUESTION}"


async def test_a_fragment_is_not_taken_as_their_answer(env: dict[str, Any]) -> None:
    """Live: "so when" (a cut-off sentence) got "Thanks, that's really helpful"."""
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    said = service.spoken_text(await _say(env, call_id, "so when"))
    assert said != script.discovery_thanks_line("Jane")  # the model handles it, and can ask again
    contact_id = await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                   call_id)
    assert all(m["text"] != "so when" for m in await memory.items(db, claims, str(contact_id)))


async def _form_call(env: dict[str, Any], tenant: str, answers: dict[str, str] | None = None,
                     ) -> tuple[str, str]:
    """A website form that asked for a call: call now, then the inbound emails. ``answers``:
    the form's other fields, noted the way intake notes them."""
    db = env["db"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    settings = AgentSettings(enabled_channels=("call", "email"), physical_address="1 Main St")
    await upsert_agent_settings(db, claims, settings)
    cid = await create_contact(db, claims, account_id=None, lead_id=None, name="Jane Doe",
                               email="jane@example.com", phone="2025550123", consent={})
    await add_identity(db, claims, cid, identity_type="phone", identity_value="2025550123")
    await set_contact_timezone(db, claims, cid, env["zone"])
    await add_consent(db, claims, cid, channel="call", status="granted",
                      basis="express_written_consent", source="form")
    await add_consent(db, claims, cid, channel="email", status="granted",
                      basis="express_consent", source="form")
    for label, value in (answers or {}).items():
        await memory.remember(db, claims, cid, channel="website", kind="need",
                              text=f"{label}: {value}")
    await planner.create_plan(db, claims, contact_id=cid, kind="inbound_auto", goal="follow_up",
                              now=datetime.now(UTC), steps=planner.call_first_steps(settings))
    await dispatch.dispatch_due_tasks(db, limit=10)
    call_id = await db.fetchval("SELECT call_id FROM call_sessions WHERE contact_id = $1", cid)
    return str(call_id), cid


async def test_a_form_that_picked_other_with_no_message_is_asked_what_they_need(
        env: dict[str, Any]) -> None:
    """Owner, 2026-11-17: "Other" with an empty message -- Rebecca asks what they're after."""
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, cid = await _form_call(env, a, {"service": "Other", "company": "Lee Roofing"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    asked = service.spoken_text(await _say(env, call_id, "yes"))
    assert asked == f"Great. {script.OTHER_SERVICE_QUESTION}"
    thanks = service.spoken_text(await _say(env, call_id, "we need a new website for the business"))
    assert thanks == script.qualify_line("Jane", _JOBS)  # plan 1.2: then what she still needs
    notes = await memory.items(db, claims, cid)
    assert notes[-1]["text"] == "we need a new website for the business"


# -- Qualification on the call (plan 1.2, owner 2026-11-17) ------------------------------------

_JOBS = "Roughly how many roofing jobs do you do a month?"
_SPEND = "What do you spend on marketing a month?"


class _Picker:
    """The tenant's model after the call: maps each answer to a choice by keyword."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def classify(self, text: str, labels: list[str], **_: Any) -> str:
        self.seen.append(text)
        answer = text.rsplit("Answer:", 1)[-1]
        return {"fifteen": "5 to 20", "thousand": "$1k to $5k"}.get(
            next((w for w in ("fifteen", "thousand") if w in answer), ""), "unclear")

    async def generate(self, *_: Any, **__: Any) -> Any:
        from api.llm.provider import Completion

        return Completion(text="They run a roofing firm.\nSentiment: positive", model="m",
                          input_tokens=1, output_tokens=1)

    async def aclose(self) -> None:
        return None


def _model_after_the_call(monkeypatch: pytest.MonkeyPatch) -> _Picker:
    from api.llm import config_repository, factory
    from api.llm.config_repository import LLMConfig

    picker = _Picker()

    async def llm(db: Any, claims: Any) -> LLMConfig:
        return LLMConfig(provider="openai", model="m", api_key="k", embedding_model="e")
    monkeypatch.setattr(config_repository, "get_llm_config", llm)
    monkeypatch.setattr(factory, "provider_for", lambda config: picker)
    return picker


async def test_rebecca_asks_two_missing_questions_after_their_need_and_scores_them_after(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """One question at a time, at most two a call, after they've said what they need; the
    answers are mapped and scored after the call, so the live call never waits on a model."""
    from api.qualification import repository as qrepo
    from api.qualification.scoring import DEFAULT_CONFIG, engagement, score

    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, cid = await _form_call(env, a, {"service": "SEO"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert service.spoken_text(await _say(env, call_id, "yes")) == f"Great. {_QUESTION}"
    first = service.spoken_text(await _say(env, call_id, "we want more roof replacement jobs"))
    assert first == script.qualify_line("Jane", _JOBS)
    second = service.spoken_text(await _say(env, call_id, "about fifteen a month"))
    assert second == f"Got it. {_SPEND}"
    done = service.spoken_text(await _say(env, call_id, "around three thousand dollars"))
    assert done == script.DISCOVERY_THANKS_LINE  # the name is used only once in a while
    assert await qrepo.get_record(db, claims, cid) == qrepo.Record()  # nothing yet: no waiting

    picker = _model_after_the_call(monkeypatch)
    await service.after_call(db, claims, {"call_id": call_id, "outcome": "completed"})
    record = await qrepo.get_record(db, claims, cid)
    assert record.chosen == {"jobs_per_month": "5 to 20", "marketing_spend": "$1k to $5k"}
    assert record.answers["jobs_per_month"]["heard"] == "about fifteen a month"
    assert record.answers["jobs_per_month"]["channel"] == "call"
    signals = await qrepo.engagement_signals(db, claims, cid)
    assert record.score == score(DEFAULT_CONFIG, record.chosen, engagement(**signals))
    assert f"Question: {_JOBS}\nAnswer: about fifteen a month" in picker.seen


async def test_a_question_instead_of_an_answer_is_answered_and_nothing_is_saved(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.qualification import repository as qrepo

    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, cid = await _form_call(env, a, {"service": "SEO"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    await _say(env, call_id, "we want more roof replacement jobs")
    reply = service.spoken_text(await _say(env, call_id, "How much does a repair cost?"))
    assert reply.startswith("We charge two hundred dollars")  # answered, not filed as a count
    _model_after_the_call(monkeypatch)
    await service.after_call(db, claims, {"call_id": call_id, "outcome": "completed"})
    assert (await qrepo.get_record(db, claims, cid)).answers == {}


async def test_a_pause_after_an_answer_asks_a_question_she_still_needs(
        env: dict[str, Any]) -> None:
    """The one follow-up after a pause (R33) is a question she still needs, when there is one;
    questions already answered (any channel) are never asked again."""
    from api.qualification import repository as qrepo

    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    cid0, _ = await _contact_with_call_task(env, a)
    for key in ("jobs_per_month", "marketing_spend", "decision_maker", "improve"):
        await qrepo.save_answer(db, claims, cid0, key, choice="x", heard="x", channel="chat",
                                at=datetime.now(UTC))
    await dispatch.dispatch_due_tasks(db, limit=10)
    call_id = await db.fetchval("SELECT call_id FROM call_sessions WHERE contact_id = $1", cid0)
    await _webhook(env, str(call_id), "answer", {"CallUUID": "uuid-1"})
    await _say(env, str(call_id), "How much does a repair cost?")
    asked = service.spoken_text(await _say(env, str(call_id), ""))
    assert asked == script.pause_question("When would you like to get started?")
    thanks = service.spoken_text(await _say(env, str(call_id), "probably in the spring"))
    assert thanks == f"Got it, thanks. {script.ANYTHING_ELSE_AFTER_PAUSE}"
    session = await repo.get_session(db, claims, str(call_id))
    assert session is not None and session.session["qual_heard"] == {
        "start": "probably in the spring"}


async def test_a_client_with_no_questions_keeps_the_plain_discovery_thanks(
        env: dict[str, Any]) -> None:
    """Tenant A's empty question list is A's alone: B's calls still ask the roofing set."""
    from api.qualification import repository as qrepo
    from api.qualification.scoring import QualConfig

    db, a, b = env["db"], env["a"], env["b"]
    await qrepo.save_config(db, AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a),
                            QualConfig(questions=[]))
    call_id, _ = await _form_call(env, a, {"service": "SEO"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    thanks = service.spoken_text(await _say(env, call_id, "we want more roof replacement jobs"))
    assert thanks == script.discovery_thanks_line("Jane")
    assert await qrepo.get_config(db, AuthClaims(subject="admin", role=Role.CLIENT_ADMIN,
                                                 tenant_id=b)) != QualConfig(questions=[])


async def test_a_form_with_a_real_service_keeps_her_own_follow_up_question(
        env: dict[str, Any]) -> None:
    call_id, _ = await _form_call(env, env["a"], {"service": "Other",
                                                  "message": "Can you redo our logo?"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert service.spoken_text(await _say(env, call_id, "yes")) == f"Great. {_QUESTION}"
    assert "redo our logo" in (env["hooks"][-1][1] or "")  # the opening is written from it


async def test_the_final_check_confirms_name_company_and_email_and_a_new_email_sticks(
        env: dict[str, Any]) -> None:
    """Owner, 2026-11-17: confirm the details from the form, ask if the email is the right
    one, and take a different one precisely -- the booking and the follow-ups go to it."""
    db, a = env["db"], env["a"]
    call_id, cid = await _form_call(env, a, {"service": "SEO", "company": "Lee Roofing"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "Sure, how much is a repair?")
    await _say(env, call_id, "I'd like to book a time")
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    three = datetime.combine(day, dtime(15), tzinfo=_NY)
    who = "Jane Doe from Lee Roofing"
    final = service.spoken_text(await _say(env, call_id, "tomorrow at 3 pm"))
    assert final == script.final_check_line(three, "America/New_York", "jane@example.com", who=who)
    heard = service.spoken_text(await _say(
        env, call_id, "no, use k a u s t u b h dot b one at gmail dot com instead"))
    assert heard == script.final_check_line(three, "America/New_York", "kaustubh.b1@gmail.com",
                                            who=who)
    assert service.spoken_text(await _say(env, call_id, "yes that's right")).startswith(
        "You're booked for")
    assert await db.fetchval("SELECT email FROM schedule_events WHERE tenant_id = $1",
                             a) == "kaustubh.b1@gmail.com"
    assert await db.fetchval("SELECT email FROM contacts WHERE contact_id = $1",
                             cid) == "kaustubh.b1@gmail.com"  # the follow-ups use it too


async def _at_final_check(env: dict[str, Any], tenant: str) -> tuple[str, str, datetime]:
    call_id, cid = await _form_call(env, tenant, {"service": "SEO", "company": "Lee Roofing"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "Sure, how much is a repair?")
    await _say(env, call_id, "I'd like to book a time")
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    three = datetime.combine(day, dtime(15), tzinfo=_NY)
    final = service.spoken_text(await _say(env, call_id, "tomorrow at 3 pm"))
    assert final == script.final_check_line(three, "America/New_York", "jane@example.com",
                                            who="Jane Doe from Lee Roofing")
    return call_id, cid, three


async def test_also_send_it_to_my_other_mail_asks_for_it_and_both_get_the_details(
        env: dict[str, Any]) -> None:
    """Live 2026-11-18: "send it to my other mail as well" wasn't heard as being about the
    email, so the same final check came back again and again. Now she asks for the other
    address, reads both back, and both get the booking details."""
    db, a = env["db"], env["a"]
    call_id, cid, three = await _at_final_check(env, a)
    asked = service.spoken_text(await _say(env, call_id, "Can you also send it to my other mail?"))
    assert asked == script.EXTRA_EMAIL_ASK
    both = service.spoken_text(await _say(env, call_id, "v i n i t h at isngs dot com"))
    assert both == script.final_check_line(three, "America/New_York", "jane@example.com",
                                           who="Jane Doe from Lee Roofing",
                                           extra="vinith@isngs.com")
    assert service.spoken_text(await _say(env, call_id, "yes")).startswith("You're booked for")
    sent = {r["recipient"] for r in await db.fetch(
        "SELECT recipient FROM notification_jobs WHERE tenant_id = $1 AND "
        "payload->>'kind' = 'booking_confirm'", a)}
    assert sent == {"jane@example.com", "vinith@isngs.com"}
    assert await db.fetchval("SELECT email FROM contacts WHERE contact_id = $1",
                             cid) == "jane@example.com"  # a copy, not a replacement
    assert await db.fetchval("SELECT contact_id FROM contact_identities WHERE tenant_id = $1 "
                             "AND identity_value = 'vinith@isngs.com'", a) == cid


async def test_an_instruction_at_the_final_check_is_never_taken_as_the_email(
        env: dict[str, Any]) -> None:
    """Live 2026-11-18: "Don't change anything, send me the booking link to it at isngs.com"
    became don'tchangeanything.sendmethebookinglinktoit@isngs.com. Now a domain without a
    clear address gets "please spell it", and the spelled address is the one used."""
    db, a = env["db"], env["a"]
    call_id, _, three = await _at_final_check(env, a)
    retry = service.spoken_text(await _say(
        env, call_id, "Don't change anything, send me the booking link to it at isngs.com"))
    assert retry == script.EMAIL_RETRY_LINE
    fixed = service.spoken_text(await _say(env, call_id, "v i n i t h at i s n g s dot com"))
    assert fixed == script.final_check_line(three, "America/New_York", "vinith@isngs.com",
                                            who="Jane Doe from Lee Roofing")
    await _say(env, call_id, "yes")
    assert await db.fetchval("SELECT email FROM schedule_events WHERE tenant_id = $1",
                             a) == "vinith@isngs.com"


async def test_the_final_check_is_never_read_twice_in_a_row(env: dict[str, Any]) -> None:
    call_id, _, three = await _at_final_check(env, env["a"])
    unsure = service.spoken_text(await _say(env, call_id, "hmm let me think about it"))
    assert unsure == script.CONFIRM_CLARIFY_LINE  # not the whole read-back again
    again = service.spoken_text(await _say(env, call_id, "hmm let me think about it"))
    assert again == script.final_check_line(three, "America/New_York", "jane@example.com",
                                            who="Jane Doe from Lee Roofing")


async def test_a_call_booking_confirms_with_their_agenda_a_prep_question_and_a_manage_link(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan 1.6: the confirmation for a call booked on the phone carries what they asked about
    (the form), one question the team still needs, and the link to move or cancel it."""
    import re

    from api.scheduling import manage

    db, a = env["db"], env["a"]
    monkeypatch.setenv("EMAIL_LINK_SECRET", "l" * 40)
    get_api_settings.cache_clear()
    call_id, _ = await _form_call(env, a, {"service": "SEO", "message": "We need more leads"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "Sure, how much is a repair?")
    await _say(env, call_id, "I'd like to book a time")
    await _say(env, call_id, "tomorrow at 3 pm")
    assert service.spoken_text(await _say(env, call_id, "yes that's right")).startswith(
        "You're booked for")
    body = await db.fetchval("SELECT body FROM notification_jobs WHERE tenant_id = $1 AND "
                             "payload->>'kind' = 'booking_confirm'", a)
    assert "What we'll cover:\n- SEO\n- We need more leads" in body
    assert ("To help us prepare, could you reply with: Roughly how many roofing jobs do you do "
            "a month?") in body
    token = re.search(r"/public/bookings/(\S+)", body)
    assert token is not None
    event_id = await db.fetchval("SELECT event_id FROM schedule_events WHERE tenant_id = $1", a)
    assert manage.verify(token.group(1)) == (a, event_id)


async def test_a_corrected_name_or_company_is_noted_and_never_read_back_wrong(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, cid = await _form_call(env, a, {"company": "Lee Roofing"})
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "Sure, how much is a repair?")
    await _say(env, call_id, "I'd like to book a time")
    await _say(env, call_id, "tomorrow at 3 pm")
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    three = datetime.combine(day, dtime(15), tzinfo=_NY)
    said = service.spoken_text(await _say(env, call_id, "the company is Lee Roofing and Gutters"))
    assert said == (f"{script.NOTED_LINE} "
                    f"{script.final_check_line(three, 'America/New_York', 'jane@example.com')}")
    notes = await memory.items(db, claims, cid)
    assert "Lee Roofing and Gutters" in notes[-1]["text"]


async def test_the_model_gets_the_callers_first_name(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def answer(*args: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "We charge two hundred dollars for that."

    call_id = await _answered(env, env["a"])
    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    await _say(env, call_id, "how long does a repair take")
    assert seen["caller_name"] == "Jane"


async def test_after_a_real_conversation_a_thank_you_email_goes_right_away(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    sent: list[str] = []

    async def mail(db: Any, claims: Any, task: Any, run_id: str) -> Any:
        sent.append(task.goal)
        return ExecutorResult(status="completed", result={})

    previous = executors.get_executor("email")
    executors.register("email", mail)
    try:
        call_id, _ = await _form_call(env, a)
        await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
        await _say(env, call_id, "Sure, how much is a repair?")
        await _say(env, call_id, "No, that's all, bye")
        await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "Duration": "60",
                                                "CallStatus": "completed"})
        await dispatch.dispatch_events(db, limit=20)  # after the call
        await dispatch.dispatch_due_tasks(db, limit=10)
    finally:
        if previous is not None:
            executors.register("email", previous)
        else:
            executors.unregister("email")
    assert sent == ["thank_you"]  # straight after the call, despite the usual 48 h gap
    later = [r["goal"] for r in await db.fetch(
        "SELECT goal FROM outreach_tasks WHERE tenant_id = $1 AND channel = 'email' "
        "AND status = 'scheduled' ORDER BY scheduled_at", a)]
    # Plan 1.4: the acknowledgment became the thank-you (one email, not two); the rest stay.
    assert later == ["follow_up", "re_engage"]
    assert await db.fetchval("SELECT count(*) FROM outreach_tasks WHERE tenant_id = $1",
                             env["b"]) == 0


async def test_an_unanswered_call_sends_no_thank_you(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id, _ = await _form_call(env, a)
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "CallStatus": "no-answer"})
    await dispatch.dispatch_events(db, limit=20)
    goals = [r["goal"] for r in await db.fetch(
        "SELECT goal FROM outreach_tasks WHERE tenant_id = $1 AND channel = 'email' "
        "AND scheduled_at <= now()", a)]
    assert goals == []  # nothing to thank them for; the call is retried instead


async def test_every_hangup_logs_who_ended_the_call_and_why(
        env: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    """2026-10-29: a call dropped after the opening and nothing on our side said why."""
    call_id, _ = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    with caplog.at_level("INFO", logger="api.call_agent.service"):
        await _webhook(env, call_id, "hangup", {
            "CallUUID": "uuid-1", "CallStatus": "completed", "Duration": "26",
            "HangupSource": "Caller", "HangupCauseName": "Normal Hangup"})
    ended = [r for r in caplog.records if getattr(r, "event", None) == "call_hangup"]
    assert len(ended) == 1
    assert ended[0].reason == "Caller: Normal Hangup after 0 turns"  # type: ignore[attr-defined]


async def test_a_calendar_that_refuses_the_booking_undoes_it_and_hands_off(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.scheduling.calendar_config_repository import upsert_calendar_config

    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    await upsert_calendar_config(db, claims, provider="stub", calendar_id=None,
                                 credentials=uuid4().hex, busy=[], enabled=True)

    class _Down:
        async def free_busy(self, *args: Any) -> list[Any]:
            return []

        async def create_event(self, *args: Any) -> Any:
            raise RuntimeError("Google said no")

    async def provider(calendar: Any) -> _Down:
        return _Down()

    monkeypatch.setattr(booking, "calendar_provider", provider)
    call_id = await _answered(env, a)
    for line in ("I'd like to book a time", "tomorrow at 3 pm"):
        await _say(env, call_id, line)
    end = await _say(env, call_id, "yes")
    assert service.spoken_text(end) == script.SYNC_FAILED_LINE and "<Hangup" in end
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1", a) == 0
    assert await db.fetchval("SELECT count(*) FROM agent_events WHERE tenant_id = $1 "
                             "AND kind = 'meeting_booked'", a) == 0


class _Google:
    """A calendar that reports its owner, as Google does on a created event."""

    async def free_busy(self, *args: Any) -> list[Any]:
        return []

    async def create_event(self, claims: Any, event: Any) -> CalendarRef:
        return CalendarRef(provider="google", external_id=f"g-{event.event_id}",
                           meet_url="https://meet.google.com/abc", organizer_email=_REP)


async def _book_on_a_call(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                          *, email_on_file: bool = True) -> str:
    from api.scheduling.calendar_config_repository import upsert_calendar_config

    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    await upsert_calendar_config(db, claims, provider="stub", calendar_id=None,
                                 credentials=uuid4().hex, busy=[], enabled=True)

    async def provider(calendar: Any) -> _Google:
        return _Google()

    monkeypatch.setattr(booking, "calendar_provider", provider)
    call_id = await _answered(env, a)
    if not email_on_file:
        await db.execute("UPDATE contacts SET email = NULL WHERE tenant_id = $1", a)
    await _say(env, call_id, "I'd like to book a time")
    await _say(env, call_id, "tomorrow at 3 pm")
    return call_id


async def test_a_booking_on_a_call_tells_the_calendar_owner(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-02: the caller got a confirmation, the person taking the call got nothing.
    The widget's bookings already told them; this route never did."""
    db, a, b = env["db"], env["a"], env["b"]
    call_id = await _book_on_a_call(env, monkeypatch)
    await _say(env, call_id, "yes")

    event_id = await db.fetchval("SELECT event_id FROM schedule_events WHERE tenant_id = $1", a)
    job = await db.fetchrow("SELECT subject, body, dedupe_key FROM notification_jobs "
                            "WHERE tenant_id = $1 AND recipient = $2", a, _REP)
    assert job is not None and job["subject"] == "New call booked: Jane Doe"
    assert job["dedupe_key"] == f"booking_rep_notify:{event_id}"  # exactly once per booking
    for detail in ("jane@example.com", "+12025550123", "https://meet.google.com/abc"):
        assert detail in job["body"]
    assert "Eastern" not in job["subject"]
    assert service.send_notification.delay.call_count == 2  # the caller's and the owner's
    assert await db.fetchval("SELECT count(*) FROM notification_jobs WHERE tenant_id = $1",
                             b) == 0


async def test_the_owner_is_told_even_when_the_caller_gave_no_email(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    db, a = env["db"], env["a"]
    call_id = await _book_on_a_call(env, monkeypatch, email_on_file=False)
    # three misses (2026-11-17: one more try, spelled): booked, the team sends the details
    for line in ("I don't remember", "no idea", "not sure"):
        await _say(env, call_id, line)
    await _say(env, call_id, "yes")
    assert await db.fetchval("SELECT count(*) FROM notification_jobs WHERE tenant_id = $1 "
                             "AND recipient = $2", a, _REP) == 1


async def test_a_failed_notice_to_the_owner_never_loses_the_booking(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    db, a = env["db"], env["a"]
    real = service.enqueue_notification

    async def picky(db: Any, claims: Any, **kw: Any) -> Any:
        if kw["recipient"] == _REP:
            raise RuntimeError("queue down")
        return await real(db, claims, **kw)

    monkeypatch.setattr(service, "enqueue_notification", picky)
    call_id = await _book_on_a_call(env, monkeypatch)
    end = await _say(env, call_id, "yes")
    assert service.spoken_text(end).startswith("You're booked for")
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1", a) == 1
    assert await db.fetchval("SELECT count(*) FROM notification_jobs WHERE tenant_id = $1 "
                             "AND recipient = 'jane@example.com'", a) == 1


async def test_without_a_calendar_owner_only_the_caller_is_emailed(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id = await _answered(env, a)
    await _say(env, call_id, "I'd like to book a time")
    await _say(env, call_id, "tomorrow at 3 pm")
    await _say(env, call_id, "yes")
    recipients = [r["recipient"] for r in await db.fetch(
        "SELECT recipient FROM notification_jobs WHERE tenant_id = $1", a)]
    assert recipients == ["jane@example.com"]


async def test_dont_call_me_suppresses_and_ends_the_call(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id, task_id = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    reply = await _say(env, call_id, "Don't call me again")
    assert reply.endswith("<Hangup/></Response>")
    assert script.OPT_OUT_LINE in service.spoken_text(reply)
    suppressed = await db.fetchrow("SELECT channel, value_normalised, reason FROM suppression_list "
                                   "WHERE tenant_id = $1", a)
    assert suppressed is not None
    assert tuple(suppressed) == ("call", "+12025550123", "dnc_request")
    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT status FROM outreach_plans WHERE tenant_id = $1",
                             a) == "stopped"


async def test_voicemail_leaves_the_approved_message_and_retries(env: dict[str, Any]) -> None:
    """Plivo's async "machine" verdict only counts once nobody answers Rebecca's opening:
    then the voicemail branch runs (approved message here) and the step retries."""
    db, plivo, a = env["db"], env["plivo"], env["a"]
    call_id, task_id = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _webhook(env, call_id, "machine", {"Machine": "true", "CallUUID": "uuid-1"})
    assert plivo.calls[-1][0] != "transfer_to_url"  # no cut-off mid-greeting
    vm = await _say(env, call_id, "")  # nobody answered the opening question
    assert "an AI assistant for Acme Roofing" in service.spoken_text(vm)
    assert vm.endswith("<Hangup/></Response>")
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "Duration": "20",
                                            "CallStatus": "completed"})
    task = await db.fetchrow("SELECT status, attempts, scheduled_at FROM outreach_tasks "
                             "WHERE task_id = $1", task_id)
    assert task is not None and task["status"] == "scheduled"  # 1 of 2 attempts used
    assert task["scheduled_at"] > datetime.now(UTC) + timedelta(hours=23)
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) == "voicemail"


async def test_a_person_wrongly_judged_a_machine_keeps_talking_to_rebecca(
    env: dict[str, Any],
) -> None:
    """Live: Plivo said "machine" during Rebecca's greeting to a real person and the call
    went silent and hung up. A reply to the opening proves a person: the call goes on."""
    db, plivo, a = env["db"], env["plivo"], env["a"]
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _webhook(env, call_id, "machine", {"Machine": "true", "CallUUID": "uuid-1"})
    assert all(name != "transfer_to_url" for name, _ in plivo.calls)
    reply = await _say(env, call_id, "Yes, who is this?")
    assert not reply.endswith("<Hangup/></Response>")  # the conversation continues
    row = await db.fetchrow("SELECT outcome, amd_result FROM call_sessions WHERE call_id = $1",
                            call_id)
    assert row is not None and row["outcome"] != "voicemail" and row["amd_result"] == "human"


async def test_no_answer_on_the_last_attempt_fails_the_task(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    call_id, task_id = await _dial(env, a)
    await db.execute("UPDATE outreach_tasks SET max_attempts = 1 WHERE task_id = $1", task_id)
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "CallStatus": "no-answer"})
    assert await db.fetchval("SELECT status FROM outreach_tasks WHERE task_id = $1",
                             task_id) == "failed"


async def test_webhooks_need_the_right_ticket_and_the_tenants_own_signature(
    env: dict[str, Any],
) -> None:
    call_id, _ = await _dial(env, env["a"])
    assert (await _webhook(env, call_id, "answer", {}, ticket="forged")).status_code == 401
    assert (await _webhook(env, call_id, "answer", {}, token=_TOKENS["b"])).status_code == 401
    other = uuid4().hex
    assert (await _webhook(env, other, "answer", {}, ticket=service.ticket(call_id))
            ).status_code == 401
    assert (await _webhook(env, call_id, "answer", {})).status_code == 200


async def test_concurrency_limit_defers_without_using_an_attempt(env: dict[str, Any]) -> None:
    db, plivo, a = env["db"], env["plivo"], env["a"]
    await _dial(env, a)  # one live call; the limit is 1
    _, task2 = await _contact_with_call_task(env, a, phone="2025550124")
    await dispatch.dispatch_due_tasks(db, limit=10)
    task = await db.fetchrow("SELECT status, attempts, last_error FROM outreach_tasks "
                             "WHERE task_id = $1", task2)
    assert task is not None
    assert (task["status"], task["attempts"], task["last_error"]) == (
        "scheduled", 0, "CALL_CONCURRENCY_LIMIT")
    assert plivo.names().count("place_call") == 1


def _cookie(tenant: str, role: Role = Role.CLIENT_ADMIN) -> dict[str, str]:
    token, _ = create_access_token(AuthClaims(subject="u", role=role, tenant_id=tenant),
                                   secret=_JWT, ttl_seconds=300)
    return {"access_token": token}


async def test_admin_routes_rbac_isolation_and_readiness(env: dict[str, Any]) -> None:
    db, client, a, b = env["db"], env["client"], env["a"], env["b"]
    body = {"plivo_auth_id": "MAYYYYYYYYYYYYYYYYYY", "from_number": "(786) 555-0000",
            "company_name": "Acme Roofing"}
    agent = _cookie(a, Role.CLIENT_AGENT)
    assert (await client.put("/admin/outreach/call-config", json=body,
                             cookies=agent)).status_code == 403
    got = (await client.get("/admin/outreach/call-config", cookies=agent)).json()
    assert got["configured"] and "token-for" not in str(got)
    bad_vm = await client.put("/admin/outreach/call-config", cookies=_cookie(a), json=body | {
        "voicemail_policy": "leave_message", "voicemail_message": "Call us back!"})
    assert bad_vm.json()["error_code"] == "VOICEMAIL_NEEDS_AI_DISCLOSURE"

    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "What's your warranty?")
    lat = (await client.get("/admin/outreach/calls/latency", cookies=_cookie(a))).json()
    assert lat["turns"] >= 1 and lat["p50_ms"] is not None
    detail = (await client.get(f"/admin/outreach/calls/{call_id}", cookies=_cookie(a))).json()
    assert detail["transcript"][0]["speaker"] == "rebecca"
    # A8 U7: an agent only sees calls of contacts they can see (this one isn't theirs).
    assert (await client.get(f"/admin/outreach/calls/{call_id}",
                             cookies=agent)).status_code == 404
    assert (await client.get(f"/admin/outreach/calls/{call_id}",
                             cookies=_cookie(b))).status_code == 404

    await db.execute("DELETE FROM tenant_call_agent_configs WHERE tenant_id = $1", b)
    r = await client.put("/admin/outreach/settings", cookies=_cookie(b),
                         json={"enabled_channels": ["call"]})
    assert r.status_code == 422 and r.json()["error_code"] == "CALL_NOT_READY"


# -- They already booked a call on the website form (roofengine, 2026-11-06) ----------------


async def _form_booking(env: dict[str, Any], tenant: str, at: datetime) -> str:
    from api.scheduling.repository import record_form_booking

    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    event_id = await record_form_booking(
        env["db"], claims, ref=f"form:{uuid4().hex}", starts_at=at,
        ends_at=at + timedelta(minutes=30), timezone="America/New_York", lead_id=uuid4().hex,
        email="jane@example.com", name="Jane Doe", consent={"granted": True})
    assert event_id is not None
    return event_id


async def test_a_booked_lead_is_asked_to_keep_or_reschedule_not_to_book(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(15),
                                 tzinfo=_NY)
    await _form_booking(env, a, booked_at)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})

    question = service.spoken_text(await _say(env, call_id, "yes sure"))
    assert question == f"Great. {script.booked_question(booked_at, 'America/New_York')}"
    kept = service.spoken_text(await _say(env, call_id, "keep it"))
    assert kept == script.kept_line(booked_at, "America/New_York")
    assert "find a time" not in service.spoken_text(await _say(env, call_id, "yes"))
    events = await db.fetch("SELECT status FROM schedule_events WHERE tenant_id = $1", a)
    assert [e["status"] for e in events] == ["booked"]  # nothing new, nothing cancelled


async def _booked_call(env: dict[str, Any], tenant: str) -> tuple[str, str]:
    """A contact who booked on the website, on a call, past "keep it": (call_id, event_id)."""
    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(15),
                                 tzinfo=_NY)
    event_id = await _form_booking(env, tenant, booked_at)
    call_id, _ = await _dial(env, tenant)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes sure")
    await _say(env, call_id, "keep it")
    return call_id, event_id


async def _resent_to(env: dict[str, Any], tenant: str) -> list[tuple[str, str]]:
    return [(r["recipient"], r["dedupe_key"]) for r in await env["db"].fetch(
        "SELECT recipient, dedupe_key FROM notification_jobs WHERE tenant_id = $1 "
        "AND dedupe_key LIKE 'booking_confirm:%:resend:%' ORDER BY created_at", tenant)]


async def test_the_meeting_link_is_sent_again_to_the_gmail_they_name(
        env: dict[str, Any]) -> None:
    """Live call 2026-11-19: "can you once again send me the link" got "I don't have a link to
    share". With a booking, it's the booking's confirmation, sent again -- to the address they
    name ("my gmail": their Gmail on file), and only for this tenant."""
    a, b = env["a"], env["b"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, event_id = await _booked_call(env, a)
    contact_id = await env["db"].fetchval(
        "SELECT contact_id FROM call_sessions WHERE call_id = $1", call_id)
    await add_identity(env["db"], claims, str(contact_id), identity_type="email",
                       identity_value="jane.doe@gmail.com")

    said = service.spoken_text(await _say(
        env, call_id, "Can you send me the meeting link again to my Gmail?"))
    assert said == script.link_sent_line("jane.doe@gmail.com", link=False)
    assert await _resent_to(env, a) == [
        ("jane.doe@gmail.com", f"booking_confirm:{event_id}:resend:{call_id}:jane.doe@gmail.com")]
    assert await _resent_to(env, b) == []


async def test_the_link_goes_to_the_booking_email_when_they_name_none(
        env: dict[str, Any]) -> None:
    a = env["a"]
    call_id, event_id = await _booked_call(env, a)
    said = service.spoken_text(await _say(env, call_id, "So can you once again send me the link"))
    assert said == script.link_sent_line("jane@example.com", link=False)
    assert [to for to, _ in await _resent_to(env, a)] == ["jane@example.com"]


async def test_a_mailbox_we_dont_have_is_asked_for_then_used(env: dict[str, Any]) -> None:
    a = env["a"]
    call_id, _ = await _booked_call(env, a)
    ask = service.spoken_text(await _say(env, call_id, "send the invite again to my yahoo"))
    assert ask == script.LINK_EMAIL_ASK
    said = service.spoken_text(await _say(env, call_id, "j a n e at yahoo dot com"))
    assert said == script.link_sent_line("jane@yahoo.com", link=False)
    assert [to for to, _ in await _resent_to(env, a)] == ["jane@yahoo.com"]


async def test_rescheduling_books_the_new_time_and_asks_the_team_to_cancel_the_old_invite(
        env: dict[str, Any]) -> None:
    db, a, b = env["db"], env["a"], env["b"]
    day = (datetime.now(_NY) + timedelta(days=2)).date()
    old = await _form_booking(env, a, datetime.combine(day, dtime(15), tzinfo=_NY))
    other = await _form_booking(env, b, datetime.combine(day, dtime(16), tzinfo=_NY))
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})

    # Straight to it, without the "have you got a minute?" yes.
    ask = service.spoken_text(await _say(env, call_id, "actually I need to reschedule my call"))
    assert ask == script.OTHER_TIME_LINE
    new_day = day + timedelta(days=1)
    new = datetime.combine(new_day, dtime(16), tzinfo=_NY)
    final = service.spoken_text(await _say(env, call_id, f"{new_day:%B} {new_day.day} at 4 pm"))
    assert final == _final(new, "America/New_York", "jane@example.com")
    booked = service.spoken_text(await _say(env, call_id, "yes"))
    assert booked.startswith("You're booked for")

    rows = {r["event_id"]: (r["status"], r["source"], r["starts_at"]) for r in await db.fetch(
        "SELECT event_id, status, source, starts_at FROM schedule_events WHERE tenant_id = $1", a)}
    assert rows.pop(old)[0] == "cancelled"
    assert list(rows.values()) == [("booked", "native", new)]
    assert await db.fetchval("SELECT status FROM schedule_events WHERE event_id = $1",
                             other) == "booked"  # another tenant's booking is untouched
    alert = await db.fetchrow("SELECT payload FROM notification_events WHERE tenant_id = $1 "
                              "AND kind = 'agent_call_needs_human'", a)
    assert alert is not None and alert["payload"]["reason"] == "booking_rescheduled"
    assert alert["payload"]["old_starts_at"] == datetime.combine(
        day, dtime(15), tzinfo=_NY).astimezone(UTC).isoformat()


# -- Speed test: two-way audio streaming for listed tenants only (2026-11-07) ---------------


async def test_listed_tenants_get_the_audio_stream_and_others_keep_the_xml_calls(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = env["a"], env["b"]
    for key, value in {"REALTIME_VOICE_TENANTS": a, "DEEPGRAM_API_KEY": uuid4().hex,
                       "ELEVENLABS_API_KEY": uuid4().hex, "ELEVENLABS_VOICE_ID": "voice-1"}.items():
        monkeypatch.setenv(key, value)
    get_api_settings.cache_clear()

    call_a, _ = await _dial(env, a)
    streamed = await _webhook(env, call_a, "answer", {"CallUUID": "uuid-1"})
    assert "<Stream" in streamed.text and 'bidirectional="true"' in streamed.text
    assert f"/public/call-agent/calls/{call_a}/stream?ticket=" in streamed.text
    assert "<GetInput" not in streamed.text
    opening = await env["db"].fetchval(
        "SELECT text FROM call_transcript_turns WHERE call_id = $1 AND speaker = 'rebecca'",
        call_a)
    assert opening and opening.startswith("Hi Jane, Rebecca here")  # said by the stream

    call_b, _ = await _dial(env, b)
    classic = await _webhook(env, call_b, "answer", {"CallUUID": "uuid-2"},
                             token=_TOKENS["b"])
    assert "<GetInput" in classic.text and "<Stream" not in classic.text

    from types import SimpleNamespace

    from api.call_agent import routes

    class _Ws:
        def __init__(self, ticket: str) -> None:
            self.query_params = {"ticket": ticket}
            self.app = SimpleNamespace(state=SimpleNamespace(db=env["db"]))
            self.accepted, self.closed_with = False, None

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, code: int = 1000) -> None:
            self.closed_with = code

    for call_id, ticket in ((call_a, "forged"), (call_b, service.ticket(call_b))):
        ws = _Ws(ticket)  # a forged ticket / a tenant that isn't listed: refused unopened
        await routes.stream(ws, call_id)  # type: ignore[arg-type]
        assert (ws.accepted, ws.closed_with) == (False, 1008)


async def test_asking_about_the_service_after_the_opening_gets_an_answer_not_a_thank_you(
        env: dict[str, Any]) -> None:
    """Live 2026-11-12: "So I want to know about roofing thing." -> "Thanks, that's really
    helpful. Would you like me to find a time..." -- it was a question, not their need."""
    call_id, _ = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert service.spoken_text(await _say(env, call_id, "yes")) == f"Great. {_QUESTION}"
    answer = service.spoken_text(await _say(env, call_id, "So I want to know about roofing thing."))
    # the knowledge-base answer, nothing tacked on (2026-11-16: too many "anything else?")
    assert answer == "We charge two hundred dollars for that."  # no question tacked on
    assert answer != script.discovery_thanks_line("Jane")


@pytest.mark.parametrize("goodbye", ["No. Thank you. I will talk with your team on Monday.",
                                     "no thank you, see you on the call",
                                     # Live 2026-11-14: she said goodbye but never hung up.
                                     "Nothing. I've got the answers for all the questions.",
                                     "I got all the answers for the questions."])
async def test_a_booked_lead_saying_goodbye_is_not_a_transfer_or_a_no(
        env: dict[str, Any], goodbye: str) -> None:
    """Live 2026-11-12: with a call booked, "No. Thank you. I will talk with your team on
    Monday." was taken as "put me through to a person": team line + escalated."""
    db, a = env["db"], env["a"]
    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(15),
                                 tzinfo=_NY)
    await _form_booking(env, a, booked_at)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    await _say(env, call_id, "keep it")
    end = await _say(env, call_id, goodbye)
    # the goodbye names their booked call: that's where the detail comes
    assert service.spoken_text(end) == script.booked_goodbye_line(booked_at, "America/New_York")
    assert end.endswith("<Hangup/></Response>")
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) == "completed"
    assert await db.fetchval("SELECT count(*) FROM notification_events WHERE tenant_id = $1 "
                             "AND kind = 'agent_call_needs_human'", a) == 0


async def test_a_question_about_booked_inspections_is_answered_not_a_new_booking(
        env: dict[str, Any]) -> None:
    """Live 2026-11-13, replayed: with Monday booked, "do you guarantee book inspection?"
    started a booking; "I have already booked, I don't want to repeat it" was ignored twice
    and the call ended escalated."""
    a = env["a"]
    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(9),
                                 tzinfo=_NY)
    await _form_booking(env, a, booked_at)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    await _say(env, call_id, "keep that")
    answer = service.spoken_text(await _say(env, call_id, "So do you guarantee book inspection?"))
    # the knowledge-base answer, nothing tacked on (2026-11-16: too many "anything else?")
    assert answer == "We charge two hundred dollars for that."  # no question tacked on
    assert script.WHICH_TIME not in answer


async def test_no_thanks_mid_booking_leaves_the_booking_flow(env: dict[str, Any]) -> None:
    call_id = await _answered(env, env["a"])
    assert service.spoken_text(await _say(env, call_id, "I'd like to book a time")) == (
        script.ASK_TIME_LINE)
    out = service.spoken_text(await _say(
        env, call_id, "No. I have already booked up also. I don't want to repeat it."))
    assert out == f"No problem. {script.ANYTHING_ELSE}"
    assert await env["db"].fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                                    call_id) != "escalated"


async def test_a_short_reply_to_her_own_farewell_ends_the_call(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-14: the model said "Have a good day.", the caller said "Mhmm." twice and
    she kept saying goodbye until the caller hung up."""
    async def answer(*args: Any, **kwargs: Any) -> str | None:
        return "Glad to hear it. Have a good day."

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "The roofing part was explained well by your website")
    end = await _say(env, call_id, "Mhmm.")
    assert service.spoken_text(end) == script.GOODBYE_LINE
    assert end.endswith("<Hangup/></Response>")


async def test_yes_to_her_own_question_is_answered_not_asked_again(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-14: "Would you like to know what it can handle...?" -> "Yes." ->
    "Great! What questions can I answer?". The yes now goes to the answer, with her question."""
    asked: list[str] = []

    async def answer(db: Any, claims: Any, question: str, *args: Any, **kwargs: Any) -> str | None:
        asked.append(question)
        return ("It answers questions and books inspections." if "strong" not in question else
                "I only know what it does. Would you like to know what it can handle, like "
                "qualifying leads?")

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    call_id = await _answered(env, env["a"])
    first = service.spoken_text(await _say(env, call_id, "So how strong is the AI assistant?"))
    assert first.endswith("like qualifying leads?"), first
    out = service.spoken_text(await _say(env, call_id, "Yes."))
    assert out.startswith("It answers questions and books inspections.")
    assert "Would you like to know what it can handle, like qualifying leads?" in asked[-1]


async def test_a_crm_question_and_a_booking_decline_keep_the_booking(
        env: dict[str, Any]) -> None:
    """Live 2026-11-14, replayed: "do I need to change my CRM?" started a reschedule, and
    "No. I don't want to book a call." hung up and put the number on do-not-call."""
    a, db = env["a"], env["db"]
    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(9),
                                 tzinfo=_NY)
    await _form_booking(env, a, booked_at)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    await _say(env, call_id, "keep that")
    answer = service.spoken_text(await _say(env, call_id, "Do I need to change my CRM?"))
    assert script.OTHER_TIME_LINE not in answer and script.WHICH_TIME not in answer
    out = service.spoken_text(await _say(env, call_id, "No. I don't want to book a call."))
    assert script.OPT_OUT_LINE not in out
    assert await db.fetchval("SELECT count(*) FROM suppression_list WHERE tenant_id = $1", a) == 0
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) != "opted_out"


async def test_questions_about_the_booked_call_are_answered_from_the_booking(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-13: "How long would the call be?" -> "a good question for our team".
    The booking's own facts now reach the answer."""
    seen: list[str | None] = []

    async def answer(db: Any, claims: Any, question: str, history: Any, *, model: Any = None,
                     about_caller: Any = None, can_book: bool = False,
                     assistant: Any = None, **_: Any) -> str | None:
        seen.append(about_caller)
        return "It's a 30-minute call with our team."

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(9),
                                 tzinfo=_NY)
    await _form_booking(env, env["a"], booked_at)
    call_id, _ = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    await _say(env, call_id, "keep it")
    assert service.spoken_text(await _say(env, call_id, "How long would the call be?")
                               ) == "It's a 30-minute call with our team."
    facts = seen[-1] or ""
    assert "30 minutes" in facts and script.say_time(booked_at, "America/New_York") in facts


async def test_with_a_call_booked_unanswered_questions_go_to_the_team_not_a_hand_off(
        env: dict[str, Any]) -> None:
    """Live 2026-11-13: two questions the knowledge base can't answer, then "Okay." -> she
    ended the call, "someone from our team will get back to you", escalated."""
    db, a = env["db"], env["a"]
    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(9),
                                 tzinfo=_NY)
    await _form_booking(env, a, booked_at)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    await _say(env, call_id, "keep it")
    said = [service.spoken_text(await _say(env, call_id, question))
            for question in ("Is there a warranty on the ads?",
                             "And a warranty on the landing page?")]
    assert all(any(line in s for line in script.MISS_LINES) for s in said)
    assert said[0] != said[1]  # 2026-11-16: never the same line twice in a row
    after = await _say(env, call_id, "Okay.")
    assert service.spoken_text(after) == "Great! What questions can I answer?"
    assert not after.endswith("<Hangup/></Response>")
    assert await db.fetchval("SELECT count(*) FROM notification_events WHERE tenant_id = $1 "
                             "AND kind = 'agent_call_needs_human'", a) == 0
    contact_id = await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                   call_id)
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    asked = [i["text"] for i in await memory.items(db, claims, str(contact_id))
             if i["kind"] == "asked"]
    assert asked == ["Is there a warranty on the ads?", "And a warranty on the landing page?"]


async def test_listed_tenants_calls_answer_with_the_server_claude_setting(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-11-13: calls on Claude (server setting) while the tenant's chatbot keeps its own
    AI; another tenant's calls are untouched."""
    from api.llm.config_repository import upsert_llm_config

    a, b = env["a"], env["b"]
    for tenant in (a, b):
        await upsert_llm_config(env["db"], AuthClaims(subject="admin", role=Role.CLIENT_ADMIN,
                                                      tenant_id=tenant),
                                provider="openai", model="gpt-6-luna", api_key=uuid4().hex,
                                base_url=None, api_version=None,
                                embedding_model="text-embedding-3-small",
                                embedding_base_url="https://api.openai.com/v1",
                                embedding_api_key=uuid4().hex, embedding_dimensions=1536)
    for key, value in {"REALTIME_VOICE_TENANTS": a, "DEEPGRAM_API_KEY": uuid4().hex,
                       "ELEVENLABS_API_KEY": uuid4().hex, "ELEVENLABS_VOICE_ID": "voice-1",
                       "ANTHROPIC_API_KEY": uuid4().hex,
                       "CALLS_LLM_MODEL": "claude-haiku-5-5"}.items():
        monkeypatch.setenv(key, value)
    get_api_settings.cache_clear()
    used: list[Any] = []

    async def answer(db: Any, claims: Any, question: str, history: Any, *, model: Any = None,
                     about_caller: Any = None, can_book: bool = False,
                     llm_config: Any = None, assistant: Any = None,
                     **_: Any) -> str | None:
        used.append((claims.tenant_id, llm_config))
        return "We charge two hundred dollars for that."

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    for tenant, token in ((a, _TOKENS["a"]), (b, _TOKENS["b"])):
        call_id, _ = await _dial(env, tenant)
        await _webhook(env, call_id, "answer", {"CallUUID": f"uuid-{tenant}"}, token=token)
        await _webhook(env, call_id, "turn", {"Speech": "How much is a repair?",
                                              "CallUUID": f"uuid-{tenant}"}, token=token)
    by_tenant = dict(used)
    assert by_tenant[a] is not None
    assert (by_tenant[a].provider, by_tenant[a].model) == ("anthropic", "claude-haiku-5-5")
    assert by_tenant[a].embedding_base_url == "https://api.openai.com/v1"
    assert by_tenant[b] is None  # not listed: its own AI setting, exactly as before


async def test_a_voicemail_greeting_gets_the_approved_message_not_a_reply(
        env: dict[str, Any]) -> None:
    """Live 2026-11-14: CallHippo's "We are not available, please leave a message" got "I'm
    sorry, I'm the AI assistant and I can't take messages...". It's a machine: leave the
    approved voicemail and retry later -- whatever Plivo's own detector said."""
    db = env["db"]
    call_id, task_id = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    vm = await _say(env, call_id, "Hi. We are not available at the moment. Please leave a "
                                  "message, and we will get back to you.")
    assert "an AI assistant for Acme Roofing" in service.spoken_text(vm)
    assert vm.endswith("<Hangup/></Response>")
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) == "voicemail"


async def test_a_misheard_question_never_reaches_the_notes_after_the_call(
        env: dict[str, Any]) -> None:
    """Live 2026-11-14/15: "Meta ads" heard as "my dad"; she asked them to repeat it, but the
    misheard question was saved and a later e-mail said "your dad's roofing business"."""
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, _ = await _dial(env, a)
    for speaker, text in [
            ("rebecca", "Hi, this is Rebecca."),
            ("caller", "So can you generate leads with my dad?"),
            ("rebecca", "I'm not sure what you mean. Could you say that again?"),
            ("caller", "How do you generate leads using meta ads?"),
            ("rebecca", "Meta ads run on Facebook and Instagram.")]:
        await repo.add_turn(db, claims, call_id, speaker=speaker, text=text)
    await service.after_call(db, claims, {"call_id": call_id, "outcome": "completed"})
    contact_id = await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                   call_id)
    notes = [i["text"] for i in await memory.items(db, claims, str(contact_id))]
    assert "How do you generate leads using meta ads?" in notes
    assert not any("dad" in n for n in notes)


# -- Telnyx (T1-T3, 2026-11-16): listed tenants dial, hear events and stream through Telnyx ----


class _Telnyx:
    """Records every Telnyx API call the agent makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.dialled = 0

    async def dial(self, **kw: Any) -> PlacedCall:
        self.calls.append(("dial", kw))
        self.dialled += 1
        return PlacedCall(request_uuid=f"leg-{self.dialled}", call_uuid=f"v2:call-{self.dialled}")

    async def start_recording(self, **kw: Any) -> Recording:
        self.calls.append(("start_recording", kw))
        return Recording(recording_id=f"pending:{kw['call_uuid']}", url=None)

    async def stop_recording(self, **kw: Any) -> None:
        self.calls.append(("stop_recording", kw))

    async def delete_recording(self, **kw: Any) -> None:
        self.calls.append(("delete_recording", kw))

    async def recording_ids(self, **kw: Any) -> list[str]:
        self.calls.append(("recording_ids", kw))
        return ["rec-looked-up"]

    async def hangup(self, **kw: Any) -> None:
        self.calls.append(("hangup", kw))

    async def answer(self, **kw: Any) -> None:
        self.calls.append(("answer", kw))

    async def reject(self, **kw: Any) -> None:
        self.calls.append(("reject", kw))

    async def gather_using_speak(self, **kw: Any) -> None:
        self.calls.append(("gather_using_speak", kw))

    async def speak(self, **kw: Any) -> None:
        self.calls.append(("speak", kw))

    async def fetch_recording(self, **kw: Any) -> bytes:
        self.calls.append(("fetch_recording", kw))
        return b"ID3-telnyx-audio"

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def _on_telnyx(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *tenants: str) -> Any:
    """Put ``tenants`` on Telnyx (and on the stream it needs). Returns (fake, signing key)."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    listed = ",".join(tenants)
    for key, value in {"REALTIME_VOICE_TENANTS": listed, "DEEPGRAM_API_KEY": uuid4().hex,
                       "ELEVENLABS_API_KEY": uuid4().hex, "ELEVENLABS_VOICE_ID": "voice-1",
                       "TELNYX_TENANTS": listed, "TELNYX_API_KEY": uuid4().hex,
                       "TELNYX_PUBLIC_KEY": public.decode(),
                       "TELNYX_CONNECTION_ID": "conn-1"}.items():
        monkeypatch.setenv(key, value)
    get_api_settings.cache_clear()
    fake = _Telnyx()
    monkeypatch.setattr(service, "telnyx_factory", lambda api_key, connection_id: fake)
    return fake, private


async def _telnyx_event(env: dict[str, Any], private: Any, call_id: str, event_type: str, *,
                        control_id: str = "v2:call-1", ticket: str | None = None,
                        sign_with: Any = None, **payload: Any) -> Any:
    import base64
    import json
    import time

    body = json.dumps({"data": {"record_type": "event", "event_type": event_type,
                                "id": uuid4().hex,
                                "payload": {"call_control_id": control_id, **payload}}}).encode()
    stamp = str(int(time.time()))
    signature = base64.b64encode((sign_with or private).sign(stamp.encode() + b"|" + body))
    return await env["client"].post(
        f"/public/call-agent/telnyx/calls/{call_id}/events",
        params={"ticket": ticket or service.ticket(call_id)}, content=body,
        headers={"telnyx-signature-ed25519": signature.decode(), "telnyx-timestamp": stamp,
                 "content-type": "application/json"})


async def _row(env: dict[str, Any], call_id: str) -> Any:
    return await env["db"].fetchrow("SELECT * FROM call_sessions WHERE call_id = $1", call_id)


async def test_a_listed_tenant_is_dialled_through_telnyx_and_others_stay_on_plivo(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = env["a"], env["b"]
    fake, _ = _on_telnyx(env, monkeypatch, a)
    call_a, _ = await _dial(env, a)
    [(name, sent)] = fake.calls
    assert name == "dial" and env["plivo"].calls == []
    assert (sent["to_e164"], sent["from_e164"], sent["time_limit"]) == (
        "+12025550123", "+17865550000", 300)  # tenant A's own number
    assert sent["webhook_url"] == (f"{_BASE}/public/call-agent/telnyx/calls/{call_a}/events"
                                   f"?ticket={service.ticket(call_a)}")
    assert sent["stream_url"] == (f"wss://api.example.com/public/call-agent/calls/{call_a}/stream"
                                  f"?ticket={service.ticket(call_a)}")
    row = await _row(env, call_a)
    assert (row["provider_call_uuid"], row["provider_request_uuid"], row["from_e164"]) == (
        "v2:call-1", "leg-1", "+17865550000")

    await _dial(env, b)  # not listed: Plivo, as before
    assert env["plivo"].names() == ["place_call"] and fake.dialled == 1


async def test_telnyx_events_drive_the_same_call_lifecycle(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a, db = env["a"], env["db"]
    fake, key = _on_telnyx(env, monkeypatch, a)
    call_id, task_id = await _dial(env, a)

    for _ in range(2):  # Telnyx may deliver an event twice: the opening is written once
        assert (await _telnyx_event(env, key, call_id, "call.answered")).status_code == 200
    row = await _row(env, call_id)
    assert row["status"] == "in_progress" and row["answered_at"] is not None
    turns = await db.fetch("SELECT speaker, text FROM call_transcript_turns WHERE call_id = $1",
                           call_id)
    assert [t["speaker"] for t in turns] == ["rebecca"]
    assert turns[0]["text"].startswith("Hi Jane, Rebecca here") and _HOOK in turns[0]["text"]

    await _telnyx_event(env, key, call_id, "call.machine.detection.ended", result="human")
    assert (await _row(env, call_id))["amd_result"] is None
    await _telnyx_event(env, key, call_id, "call.machine.detection.ended", result="machine")
    assert (await _row(env, call_id))["amd_result"] == "machine"

    # The recording is named only when saved: stored by id (its download links expire).
    await _telnyx_event(env, key, call_id, "call.recording.saved", recording_id="rec-9",
                        recording_urls={"mp3": "https://files.example.com/x.mp3"})
    row = await _row(env, call_id)
    assert (row["recording_id"], row["recording_url"]) == ("rec-9", "telnyx:rec-9")
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    assert await service.recording_audio(db, claims, call_id) == b"ID3-telnyx-audio"
    assert fake.calls[-1] == ("fetch_recording", {"url": "telnyx:rec-9"})

    for _ in range(2):  # and a hangup settles the call once
        await _telnyx_event(env, key, call_id, "call.hangup", hangup_cause="normal_clearing",
                            hangup_source="callee")
    row = await _row(env, call_id)
    assert (row["status"], row["outcome"]) == ("completed", "completed")
    assert row["ended_at"] is not None and row["duration_s"] is not None
    assert await db.fetchval("SELECT count(*) FROM agent_events WHERE kind = 'call_completed' "
                             "AND tenant_id = $1", a) == 1
    assert (await db.fetchrow("SELECT status FROM outreach_tasks WHERE task_id = $1",
                              task_id))["status"] == "completed"


async def test_an_unanswered_telnyx_call_is_retried_like_any_other(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a = env["a"]
    _, key = _on_telnyx(env, monkeypatch, a)
    call_id, task_id = await _dial(env, a)
    await _telnyx_event(env, key, call_id, "call.hangup", hangup_cause="timeout",
                        hangup_source="unknown")
    row = await _row(env, call_id)
    assert (row["status"], row["outcome"]) == ("no_answer", "no_answer")
    task = await env["db"].fetchrow("SELECT status, last_error FROM outreach_tasks "
                                    "WHERE task_id = $1", task_id)
    assert (task["status"], task["last_error"]) == ("scheduled", "CALL_NO_ANSWER")


async def test_a_recording_saved_after_they_objected_is_deleted_not_kept(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a, db = env["a"], env["db"]
    fake, key = _on_telnyx(env, monkeypatch, a)
    call_id, _ = await _dial(env, a)
    await _telnyx_event(env, key, call_id, "call.answered")
    await db.execute("UPDATE call_sessions SET recording_consent = false WHERE call_id = $1",
                     call_id)
    await _telnyx_event(env, key, call_id, "call.recording.saved", recording_id="rec-9")
    await _telnyx_event(env, key, call_id, "call.recording.saved")  # no id: looked up by call
    row = await _row(env, call_id)
    assert (row["recording_id"], row["recording_url"]) == (None, None)
    assert [c for c in fake.calls if c[0] == "delete_recording"] == [
        ("delete_recording", {"recording_id": "rec-9"}),
        ("delete_recording", {"recording_id": "pending:v2:call-1"})]


async def test_forged_or_misdirected_telnyx_events_change_nothing(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    a, b = env["a"], env["b"]
    _, key = _on_telnyx(env, monkeypatch, a)
    call_a, _ = await _dial(env, a)
    call_b, _ = await _dial(env, b)  # tenant B is on Plivo

    stranger = Ed25519PrivateKey.generate()
    forged = await _telnyx_event(env, key, call_a, "call.answered", sign_with=stranger)
    bad_ticket = await _telnyx_event(env, key, call_a, "call.answered", ticket="forged")
    wrong_provider = await _telnyx_event(env, key, call_b, "call.answered")
    unknown = await _telnyx_event(env, key, "no-such-call", "call.answered",
                                  ticket=service.ticket("no-such-call"))
    for response in (forged, bad_ticket, wrong_provider, unknown):  # one answer for all
        assert response.status_code == 401
        assert response.json()["error_code"] == "TELNYX_SIGNATURE_INVALID"
    # A real, signed event about ANOTHER call, posted to this call's URL: acknowledged, ignored.
    other = await _telnyx_event(env, key, call_a, "call.hangup", control_id="v2:someone-else",
                                hangup_cause="normal_clearing")
    assert other.status_code == 200
    for call_id in (call_a, call_b):
        row = await _row(env, call_id)
        assert (row["status"], row["answered_at"], row["ended_at"]) == ("dialing", None, None)


async def test_the_telnyx_stream_says_the_opening_and_hangs_up_when_she_is_done(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The stream can connect before the "answered" event: the opening must not depend on
    it. And closing the stream doesn't end a Telnyx call -- the hang-up is ours to send."""
    from types import SimpleNamespace

    from api.call_agent import realtime, routes, telnyx

    a, db = env["a"], env["db"]
    fake, key = _on_telnyx(env, monkeypatch, a)
    call_id, _ = await _dial(env, a)
    seen: dict[str, Any] = {}

    class _Listener:
        async def close(self) -> None:
            return None

    async def connect(api_key: str, keyterms: Any) -> _Listener:
        return _Listener()

    class _Call:
        def __init__(self, socket: Any, listener: Any, voice: Any, brain: Any, *, opening: str,
                     record: Any, prefetch: Any) -> None:
            seen.update(socket=socket, opening=opening)
            assert callable(prefetch)  # 2026-11-19: early words start the knowledge lookup

        async def run(self) -> None:
            seen["ran"] = True

    monkeypatch.setattr(realtime.FluxListener, "connect", connect)
    monkeypatch.setattr(realtime, "RealtimeCall", _Call)

    class _Ws:
        query_params = {"ticket": service.ticket(call_id)}
        app = SimpleNamespace(state=SimpleNamespace(db=db))
        accepted = False

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, code: int = 1000) -> None:
            return None

    ws = _Ws()
    await routes.stream(ws, call_id)  # type: ignore[arg-type]
    assert ws.accepted and seen["ran"]
    assert isinstance(seen["socket"], telnyx.TelnyxSocket)
    assert seen["opening"].startswith("Hi Jane, Rebecca here") and _HOOK in seen["opening"]
    assert fake.calls[-1] == ("hangup", {"call_uuid": "v2:call-1"})
    row = await _row(env, call_id)
    assert row["status"] == "in_progress" and row["answered_at"] is not None

    await _telnyx_event(env, key, call_id, "call.answered")  # arrives late: nothing twice
    turns = await db.fetch("SELECT text FROM call_transcript_turns WHERE call_id = $1", call_id)
    assert [t["text"] for t in turns] == [seen["opening"]]


# -- Missed-call alert emails to the team (owner, 2026-11-16) -----------------------------------


async def _alert_to(env: dict[str, Any], tenant: str, emails: list[str]) -> None:
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    config = await repo.get_call_config(env["db"], claims)
    assert config is not None
    values = {**config.__dict__, "missed_call_alert_emails": emails}
    await repo.upsert_call_config(env["db"], claims, values=values, auth_token=None)


async def _alerts(env: dict[str, Any], tenant: str) -> list[Any]:
    return list(await env["db"].fetch(
        "SELECT recipient, subject, body FROM notification_jobs WHERE tenant_id = $1 "
        "AND payload->>'kind' = 'missed_call_alert' ORDER BY recipient", tenant))


async def test_a_missed_call_emails_the_team_to_call_the_lead(env: dict[str, Any]) -> None:
    a, b = env["a"], env["b"]
    await _alert_to(env, a, ["kaustubh@example.com", "sales@example.com"])
    await _alert_to(env, b, ["someone@other.example"])
    call_id, _ = await _dial(env, a)
    for _ in range(2):  # a repeated hangup webhook never emails twice
        await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "CallStatus": "no-answer"})
    alerts = await _alerts(env, a)
    assert [r["recipient"] for r in alerts] == ["kaustubh@example.com", "sales@example.com"]
    first = alerts[0]
    assert "Jane Doe" in first["subject"] and "call" in first["subject"].lower()
    assert "+12025550123" in first["body"] and "jane@example.com" in first["body"]
    assert "didn't pick up" in first["body"] and "attempt 1 of" in first["body"]
    assert "will try again on" in first["body"]
    assert await _alerts(env, b) == []  # another tenant's team never hears of it


async def test_a_call_that_went_to_voicemail_alerts_the_team_too(env: dict[str, Any]) -> None:
    a, db = env["a"], env["db"]
    await _alert_to(env, a, ["kaustubh@example.com"])
    call_id, _ = await _dial(env, a)
    await db.execute("UPDATE call_sessions SET outcome = 'voicemail' WHERE call_id = $1", call_id)
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "CallStatus": "completed"})
    [alert] = await _alerts(env, a)
    assert "voicemail" in alert["body"]


async def test_the_last_missed_try_says_rebecca_will_not_call_again(env: dict[str, Any]) -> None:
    a, db = env["a"], env["db"]
    await _alert_to(env, a, ["kaustubh@example.com"])
    call_id, task_id = await _dial(env, a)
    await db.execute("UPDATE outreach_tasks SET max_attempts = 1 WHERE task_id = $1", task_id)
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "CallStatus": "no-answer"})
    [alert] = await _alerts(env, a)
    assert "last try" in alert["body"] and "will try again" not in alert["body"]


async def test_a_missed_form_call_tells_the_team_the_day_4_call_is_planned(
        env: dict[str, Any]) -> None:
    """Plan 1.4: the day-0 call is one attempt and the day-4 call is the retry, so the alert
    must not say she won't call again -- it says when the next planned call is."""
    a, db = env["a"], env["db"]
    await _alert_to(env, a, ["kaustubh@example.com"])
    call_id, cid = await _form_call(env, a, {"service": "SEO"})
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "CallStatus": "no-answer"})
    [alert] = await _alerts(env, a)
    assert "last try" not in alert["body"] and "won't call them again" not in alert["body"]
    nxt = await db.fetchval(
        "SELECT min(t.scheduled_at) FROM outreach_tasks t JOIN outreach_plans p "
        "USING (tenant_id, plan_id) WHERE p.contact_id = $1 AND t.channel = 'call' "
        "AND t.status = 'scheduled'", cid)
    day = nxt.astimezone(_NY)
    assert f"Rebecca will call them again on {day:%A}, {day:%B} {day.day}." in alert["body"]
    assert await db.fetchval("SELECT status FROM outreach_tasks WHERE task_id = "
                             "(SELECT task_id FROM call_sessions WHERE call_id = $1)",
                             call_id) == "failed"  # no next-day retry of the day-0 call


async def test_no_alert_for_an_answered_call_or_without_alert_emails(env: dict[str, Any]) -> None:
    a, b = env["a"], env["b"]
    await _alert_to(env, a, ["kaustubh@example.com"])
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "CallStatus": "completed",
                                            "Duration": "40"})
    assert await _alerts(env, a) == []
    call_b, _ = await _dial(env, b)  # tenant B set no alert emails
    await _webhook(env, call_b, "hangup", {"CallUUID": "uuid-2", "CallStatus": "no-answer"},
                   token=_TOKENS["b"])
    assert await _alerts(env, b) == []


async def test_alert_emails_are_set_by_admins_and_validated(env: dict[str, Any]) -> None:
    client, a, b = env["client"], env["a"], env["b"]
    number = f"+1305{int(uuid4().hex[:6], 16) % 10_000_000:07d}"  # not left over from a run
    body = {"plivo_auth_id": "MAYYYYYYYYYYYYYYYYYY", "from_number": number,
            "company_name": "Acme Roofing", "missed_call_alert_emails": ["kaustubh@example.com"]}
    assert (await client.put("/admin/outreach/call-config", json=body,
                             cookies=_cookie(a, Role.CLIENT_AGENT))).status_code == 403
    saved = await client.put("/admin/outreach/call-config", json=body, cookies=_cookie(a))
    assert saved.status_code == 200, saved.text
    assert saved.json()["missed_call_alert_emails"] == ["kaustubh@example.com"]
    got = (await client.get("/admin/outreach/call-config", cookies=_cookie(b))).json()
    assert got["missed_call_alert_emails"] == []  # tenant B's own setting
    for bad in (["not-an-email"], [f"t{i}@example.com" for i in range(6)]):
        r = await client.put("/admin/outreach/call-config", cookies=_cookie(a),
                             json=body | {"missed_call_alert_emails": bad})
        assert r.status_code == 422



async def test_each_telnyx_tenant_calls_from_its_own_number(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-11-16: one Telnyx account, one number per tenant (spam reputation, callbacks)."""
    a, b = env["a"], env["b"]
    fake, _ = _on_telnyx(env, monkeypatch, a, b)
    claims_b = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=b)
    config_b = await repo.get_call_config(env["db"], claims_b)
    assert config_b is not None
    await repo.upsert_call_config(env["db"], claims_b, auth_token=None,
                                  values={**config_b.__dict__, "from_number": "+13055550188"})
    await _dial(env, a)
    await _dial(env, b)
    assert [kw["from_e164"] for name, kw in fake.calls if name == "dial"] == [
        "+17865550000", "+13055550188"]


# -- Callbacks to a tenant's Telnyx number (2026-11-16) ------------------------------------------


async def _numbers(env: dict[str, Any]) -> dict[str, str]:
    """Register distinct numbers for tenants A and B (inbound routing is by called number)."""
    out = {}
    for label in ("a", "b"):
        number = f"+1305{int(uuid4().hex[:6], 16) % 10_000_000:07d}"
        claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=env[label])
        await repo.register_number(env["db"], claims, number)
        out[label] = number
    return out


async def _callback_event(env: dict[str, Any], private: Any, event_type: str, *,
                          state: str | None = None, sign_with: Any = None,
                          **payload: Any) -> Any:
    import base64
    import json
    import time

    if state is not None:
        payload["client_state"] = base64.b64encode(state.encode()).decode()
    body = json.dumps({"data": {"record_type": "event", "event_type": event_type,
                                "id": uuid4().hex,
                                "payload": {"call_control_id": "v2:cb-1", **payload}}}).encode()
    stamp = str(int(time.time()))
    signature = base64.b64encode((sign_with or private).sign(stamp.encode() + b"|" + body))
    return await env["client"].post(
        "/public/call-agent/telnyx/callbacks", content=body,
        headers={"telnyx-signature-ed25519": signature.decode(), "telnyx-timestamp": stamp,
                 "content-type": "application/json"})


async def test_a_callback_hears_the_business_and_pressing_1_stops_its_calls(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a, b, db = env["a"], env["b"], env["db"]
    fake, key = _on_telnyx(env, monkeypatch, a, b)
    numbers = await _numbers(env)
    contact_id, _ = await _contact_with_call_task(env, a)  # Jane, +12025550123, in tenant A
    caller = {"from": "+12025550123", "to": numbers["a"]}

    await _callback_event(env, key, "call.initiated", direction="incoming", **caller)
    assert fake.calls[-1] == ("answer", {"call_uuid": "v2:cb-1", "client_state": "callback:answered"})
    await _callback_event(env, key, "call.answered", state="callback:answered", **caller)
    name, sent = fake.calls[-1]
    assert name == "gather_using_speak" and sent["client_state"] == "callback:menu"
    assert "Acme Roofing" in sent["text"] and "press 1" in sent["text"].lower()

    await _callback_event(env, key, "call.gather.ended", state="callback:menu", digits="1",
                          status="valid", **caller)
    name, sent = fake.calls[-1]
    assert name == "speak" and sent["client_state"] == "callback:bye"
    assert "won't call you again" in sent["text"]
    rows = await db.fetch("SELECT tenant_id, channel, value_normalised, reason, source "
                          "FROM suppression_list WHERE value_normalised = '+12025550123'")
    assert [tuple(r) for r in rows] == [(a, "call", "+12025550123", "dnc_request", "callback")]
    assert await db.fetchval("SELECT count(*) FROM agent_events WHERE tenant_id = $1 "
                             "AND kind = 'opt_out_received' AND payload->>'contact_id' = $2",
                             a, contact_id) == 1

    await _callback_event(env, key, "call.speak.ended", state="callback:bye", status="completed")
    assert fake.calls[-1] == ("hangup", {"call_uuid": "v2:cb-1"})


async def test_a_callback_without_pressing_1_changes_nothing(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a, db = env["a"], env["db"]
    fake, key = _on_telnyx(env, monkeypatch, a)
    numbers = await _numbers(env)
    caller = {"from": "+12025550123", "to": numbers["a"]}
    await _callback_event(env, key, "call.gather.ended", state="callback:menu", digits="",
                          status="timeout", **caller)
    name, sent = fake.calls[-1]
    assert (name, sent["text"], sent["client_state"]) == (
        "speak", telnyx_module().CALLBACK_BYE, "callback:bye")
    assert await db.fetchval("SELECT count(*) FROM suppression_list WHERE tenant_id = $1", a) == 0


async def test_callbacks_to_unknown_or_non_telnyx_numbers_are_rejected(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a = env["a"]
    fake, key = _on_telnyx(env, monkeypatch, a)  # tenant B stays on Plivo
    numbers = await _numbers(env)
    for to in ("+13055559999", numbers["b"]):  # nobody's number / a Plivo tenant's number
        await _callback_event(env, key, "call.initiated", direction="incoming",
                              **{"from": "+12025550123", "to": to})
        assert fake.calls[-1] == ("reject", {"call_uuid": "v2:cb-1"})
    # ... and a later step for such a number does nothing (no opt-out recorded anywhere)
    await _callback_event(env, key, "call.gather.ended", state="callback:menu", digits="1",
                          status="valid", **{"from": "+12025550123", "to": numbers["b"]})
    assert await env["db"].fetchval("SELECT count(*) FROM suppression_list "
                                    "WHERE tenant_id = ANY($1)", [a, env["b"]]) == 0


async def test_forged_callbacks_and_our_own_outbound_events_are_never_acted_on(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    a = env["a"]
    fake, key = _on_telnyx(env, monkeypatch, a)
    numbers = await _numbers(env)
    forged = await _callback_event(env, key, "call.gather.ended", state="callback:menu",
                                   digits="1", status="valid",
                                   sign_with=Ed25519PrivateKey.generate(),
                                   **{"from": "+12025550123", "to": numbers["a"]})
    assert forged.status_code == 401
    assert forged.json()["error_code"] == "TELNYX_SIGNATURE_INVALID"
    ours = await _callback_event(env, key, "call.answered", to="+12025550123")  # outbound leg
    assert ours.status_code == 200
    assert fake.calls == []
    assert await env["db"].fetchval("SELECT count(*) FROM suppression_list WHERE tenant_id = $1",
                                    a) == 0


def telnyx_module() -> Any:
    from api.call_agent import telnyx

    return telnyx


async def test_call_settings_say_which_phone_network_the_client_is_on(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-11-16: the console hides the Plivo-only fields for a client on Telnyx."""
    client, a, b = env["client"], env["a"], env["b"]
    _on_telnyx(env, monkeypatch, a)
    on_telnyx = (await client.get("/admin/outreach/call-config", cookies=_cookie(a))).json()
    assert on_telnyx["phone_provider"] == "telnyx"
    assert on_telnyx["telnyx_callback_url"] == f"{_BASE}/public/call-agent/telnyx/callbacks"
    on_plivo = (await client.get("/admin/outreach/call-config", cookies=_cookie(b))).json()
    assert on_plivo["phone_provider"] == "plivo" and "telnyx_callback_url" not in on_plivo



async def test_who_is_this_gets_her_name_and_the_business_not_the_model(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-16: "What was your name again?" -> "I don't have a personal name"."""
    asked: list[str] = []

    async def answer(db: Any, claims: Any, question: str, *args: Any, **kwargs: Any) -> str | None:
        asked.append(question)
        return "We charge two hundred dollars for that."

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    call_id = await _answered(env, env["a"])  # asks one question first
    asked.clear()
    for question in ("Who is this calling?", "What was your name again?"):
        said = service.spoken_text(await _say(env, call_id, question))
        assert said == script.who_line("Acme Roofing")
    assert asked == []


async def test_the_model_is_told_her_name_on_every_answer(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def answer(db: Any, claims: Any, question: str, *args: Any, **kwargs: Any) -> str | None:
        seen.append(kwargs.get("assistant"))
        return "We charge two hundred dollars for that."

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    call_id = await _answered(env, env["a"])  # asks one question first
    await _say(env, call_id, "How much is a roof inspection?")
    assert seen == ["Rebecca, the AI assistant at Acme Roofing"] * 2  # every answer



async def test_a_hold_request_gets_a_quiet_wait_then_one_gentle_check_in(
        env: dict[str, Any]) -> None:
    """Live 2026-11-16: "Can you hold for a 2nd?" -> "...Is there anything else you'd like to
    know?", and two 8-second silences would have hung up on a longer hold."""
    call_id = await _answered(env, env["a"])
    held = await _say(env, call_id, "Can you hold for a 2nd?")
    assert service.spoken_text(held) == script.HOLD_LINE
    for _ in range(script.HOLD_QUIET_TURNS - 1):  # quiet while they're away -- no hang-up
        quiet = await _say(env, call_id, "")
        assert service.spoken_text(quiet) == "" and "<Hangup/>" not in quiet
    check = await _say(env, call_id, "")
    assert service.spoken_text(check) == script.HOLD_CHECK_LINE
    # after the check-in, the usual rule: "are you still there?", then goodbye
    assert service.spoken_text(await _say(env, call_id, "")) == script.SILENCE_LINE
    assert (await _say(env, call_id, "")).endswith("<Hangup/></Response>")


async def test_speaking_after_a_hold_carries_on_normally(env: dict[str, Any]) -> None:
    await _no_questions(env, env["a"])
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "hold on")
    await _say(env, call_id, "")  # a quiet turn while they're away
    back = service.spoken_text(await _say(env, call_id, "Sorry, how much is a repair?"))
    assert back.startswith("We charge two hundred dollars for that.")
    # back to the usual silence rule (no long quiet wait any more): a pause after an answer
    # gets the one follow-up (R33)
    assert service.spoken_text(await _say(env, call_id, "")) == script.ANYTHING_ELSE_AFTER_PAUSE



async def test_after_an_answer_she_gives_them_space_and_asks_once_after_a_pause(
        env: dict[str, Any]) -> None:
    """Owner, 2026-11-16: "Is there anything else you'd like to know?" after every answer
    left the caller no room to talk. Now: the answer alone; one follow-up only after a pause."""
    await _no_questions(env, env["a"])
    call_id = await _answered(env, env["a"])
    answer = service.spoken_text(await _say(env, call_id, "How much is a roof inspection?"))
    assert answer == "We charge two hundred dollars for that."
    pause = service.spoken_text(await _say(env, call_id, ""))
    assert pause == script.ANYTHING_ELSE_AFTER_PAUSE
    assert (await _say(env, call_id, "")).endswith("<Hangup/></Response>")


async def test_silence_after_her_own_question_is_still_are_you_there(
        env: dict[str, Any]) -> None:
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "I'd like to book a time")  # she asks which day and time
    assert service.spoken_text(await _say(env, call_id, "")) == script.SILENCE_LINE



async def test_a_miss_uses_the_models_own_hand_off_for_that_question(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.voice_agent.agent import MissReply

    handoff = ("That's a great question about storm insurance. I believe our marketing expert "
               "will be more precise in answering that.")
    seen: list[Any] = []

    async def answer(db: Any, claims: Any, question: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("handoff"))
        return MissReply(handoff)

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    call_id = await _answered(env, env["a"])
    said = service.spoken_text(await _say(env, call_id, "Does storm insurance cover this?"))
    assert said == f"{handoff} Would you like me to find a time with them?"
    assert seen[-1] is True



async def test_you_called_me_gets_the_real_reason_for_the_call(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-16: "You had called me." -> "I don't have the details of that earlier call"."""
    asked: list[str] = []

    async def answer(db: Any, claims: Any, question: str, *args: Any, **kwargs: Any) -> Any:
        asked.append(question)
        return "We charge two hundred dollars for that."

    monkeypatch.setattr(service, "answer_from_knowledge", answer)
    call_id = await _answered(env, env["a"])
    asked.clear()
    said = service.spoken_text(await _say(env, call_id, "You had called me."))
    assert said == script.why_line("Acme Roofing", None, None)
    assert asked == []  # the code says why -- never the model's guess

    booked_at = datetime.combine((datetime.now(_NY) + timedelta(days=2)).date(), dtime(9),
                                 tzinfo=_NY)
    await _form_booking(env, env["b"], booked_at)
    call_b, _ = await _dial(env, env["b"])
    await _webhook(env, call_b, "answer", {"CallUUID": "uuid-2"}, token=_TOKENS["b"])
    turn = await _webhook(env, call_b, "turn", {"Speech": "Why are you calling?",
                                                "CallUUID": "uuid-2"}, token=_TOKENS["b"])
    assert turn.status_code == 200, turn.text
    said_b = service.spoken_text(str(turn.text))
    assert said_b == script.why_line("Acme Roofing", booked_at, "America/New_York")


async def test_rescheduling_to_the_same_time_on_another_day(env: dict[str, Any]) -> None:
    """Live 2026-11-16, replayed: "reschedule it to the 20th of November same time" ->
    "What time...?", then "Same time." three times -> "didn't catch the time" -> hand-off."""
    a = env["a"]
    day = (datetime.now(_NY) + timedelta(days=2)).date()
    await _form_booking(env, a, datetime.combine(day, dtime(10), tzinfo=_NY))
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    new_day = day + timedelta(days=3)
    new = datetime.combine(new_day, dtime(10), tzinfo=_NY)
    final = service.spoken_text(await _say(
        env, call_id, f"Can you reschedule it to the {new_day.day}th of {new_day:%B} same time?"))
    assert final == _final(new, "America/New_York", "jane@example.com")


async def test_same_time_answers_the_what_time_question(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    day = (datetime.now(_NY) + timedelta(days=2)).date()
    await _form_booking(env, a, datetime.combine(day, dtime(10), tzinfo=_NY))
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    new_day = day + timedelta(days=3)
    ask = service.spoken_text(await _say(env, call_id, f"move it to {new_day:%B} {new_day.day}"))
    assert ask == script.ask_hour_line(new_day)
    final = service.spoken_text(await _say(env, call_id, "Same time."))
    new = datetime.combine(new_day, dtime(10), tzinfo=_NY)
    assert final == _final(new, "America/New_York", "jane@example.com")
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) != "escalated"
