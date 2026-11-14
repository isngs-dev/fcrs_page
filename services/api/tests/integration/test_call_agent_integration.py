"""A6 Calling Agent on a REAL migrated Postgres: dispatcher -> Plivo (faked) ->
signed webhooks -> turns -> booking / opt-out / voicemail -> hangup settles the
task -> call_completed. Plivo's API is a recording fake; the webhook requests are
signed exactly as Plivo signs them (X-Plivo-Signature-V3, the tenant's token)."""
from __future__ import annotations

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

    async def place_call(self, **kw: Any) -> PlacedCall:
        self.calls.append(("place_call", kw))
        return PlacedCall(request_uuid="req-1")

    async def start_recording(self, **kw: Any) -> Recording:
        self.calls.append(("start_recording", kw))
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
                     about_caller: Any = None, can_book: bool = False) -> str | None:
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
    assert plivo.names().count("start_recording") == 1

    await _say(env, call_id, "Please don't record this.")
    assert plivo.names()[-2:] == ["stop_recording", "delete_recording"]
    assert await db.fetchval("SELECT recording_consent FROM call_sessions WHERE call_id = $1",
                             call_id) is False

    ask = service.spoken_text(await _say(env, call_id, "I'd like to book a time"))
    assert ask == script.ASK_TIME_LINE
    confirm = service.spoken_text(await _say(env, call_id, "whatever is earliest"))
    assert confirm.startswith("Just to confirm") and "jane at example dot com" in confirm
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
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    assert service.spoken_text(await _say(env, call_id, "yes sure")) == f"Great. {_QUESTION}"
    thanks = service.spoken_text(await _say(env, call_id, "the roof leaks over the kitchen"))
    assert thanks == script.DISCOVERY_THANKS_LINE
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
    assert confirm == script.final_check_line(three, "America/New_York", "jane@example.com")
    assert service.spoken_text(await _say(env, call_id, "no, the email")) == script.EMAIL_ASK_LINE
    heard = service.spoken_text(await _say(env, call_id, "it's jane dot doe at the rate gmail "
                                                         "dot com"))
    assert heard == script.final_check_line(three, "America/New_York", "jane.doe@gmail.com")
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
    assert final == script.final_check_line(slot, "America/New_York", "jane@example.com")

    end = await _say(env, call_id, "yes")
    assert service.spoken_text(end) == script.booked_line(slot, "America/New_York")
    assert end.endswith("<Hangup/></Response>")  # no "anything else?": the call ends here
    event = await db.fetchrow("SELECT starts_at, timezone, email FROM schedule_events "
                              "WHERE tenant_id = $1", a)
    assert event is not None and event["starts_at"] == slot
    assert (event["timezone"], event["email"]) == ("America/New_York", "jane@example.com")
    assert await db.fetchval("SELECT outcome FROM call_sessions WHERE call_id = $1",
                             call_id) == "booked"


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
    assert final == script.final_check_line(datetime.combine(day, dtime(18, 30), tzinfo=_NY),
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
    assert final == script.final_check_line(datetime.combine(day, dtime(18, 30), tzinfo=_NY),
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
    assert final == script.final_check_line(datetime.combine(day, dtime(15), tzinfo=_NY),
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
    assert final == script.final_check_line(slot, "America/Los_Angeles", "jane@example.com")
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
    assert final == script.final_check_line(three + timedelta(hours=1), "America/New_York",
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
    assert final == script.final_check_line(slot, "America/New_York", "jane@example.com")
    assert service.spoken_text(await _say(env, call_id, "yes")).startswith("You're booked for")


async def test_the_earliest_time_is_given_only_when_they_ask_for_it(env: dict[str, Any]) -> None:
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "I'd like to book a time")
    final = service.spoken_text(await _say(env, call_id, "whatever is earliest"))
    assert final.startswith("Just to confirm") and final.endswith("Shall I book it?")


async def test_asking_if_a_time_is_available_checks_that_time(env: dict[str, Any]) -> None:
    call_id = await _answered(env, env["a"])
    await _say(env, call_id, "I'd like to book a time")
    day = (datetime.now(_NY) + timedelta(days=1)).date()
    final = service.spoken_text(await _say(env, call_id, "is tomorrow at 3 pm available"))
    assert final == script.final_check_line(datetime.combine(day, dtime(15), tzinfo=_NY),
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
    assert final == script.final_check_line(three, "America/New_York", "jane.doe@gmail.com")
    moved = service.spoken_text(await _say(env, call_id, "no make it 4 pm"))
    assert moved == script.final_check_line(three + timedelta(hours=1), "America/New_York",
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
    assert final.endswith("jane at gmail dot com. Shall I book it?")


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


async def test_a_fragment_is_not_taken_as_their_answer(env: dict[str, Any]) -> None:
    """Live: "so when" (a cut-off sentence) got "Thanks, that's really helpful"."""
    db, a = env["db"], env["a"]
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=a)
    call_id, _ = await _dial(env, a)
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "yes")
    said = service.spoken_text(await _say(env, call_id, "so when"))
    assert said != script.DISCOVERY_THANKS_LINE  # the model handles it, and can ask again
    contact_id = await db.fetchval("SELECT contact_id FROM call_sessions WHERE call_id = $1",
                                   call_id)
    assert all(m["text"] != "so when" for m in await memory.items(db, claims, str(contact_id)))


async def _form_call(env: dict[str, Any], tenant: str) -> tuple[str, str]:
    """A website form that asked for a call: call now, then the inbound emails."""
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
    await planner.create_plan(db, claims, contact_id=cid, kind="inbound_auto", goal="follow_up",
                              now=datetime.now(UTC), steps=planner.call_first_steps(settings))
    await dispatch.dispatch_due_tasks(db, limit=10)
    call_id = await db.fetchval("SELECT call_id FROM call_sessions WHERE contact_id = $1", cid)
    return str(call_id), cid


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
    later = await db.fetchval("SELECT goal FROM outreach_tasks WHERE tenant_id = $1 AND "
                              "channel = 'email' AND status = 'scheduled'", a)
    assert later == "re_engage"  # the last friendly check-in stays where it was
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
    for line in ("I don't remember", "no idea"):  # two misses: booked, the team sends details
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
    assert final == script.final_check_line(new, "America/New_York", "jane@example.com")
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
    assert answer == "We charge two hundred dollars for that."  # the knowledge-base answer
    assert answer != script.DISCOVERY_THANKS_LINE


@pytest.mark.parametrize("goodbye", ["No. Thank you. I will talk with your team on Monday.",
                                     "no thank you, see you on the call"])
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
    assert service.spoken_text(end) == script.GOODBYE_LINE
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
    assert answer == "We charge two hundred dollars for that."  # the knowledge-base answer
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


async def test_questions_about_the_booked_call_are_answered_from_the_booking(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-13: "How long would the call be?" -> "a good question for our team".
    The booking's own facts now reach the answer."""
    seen: list[str | None] = []

    async def answer(db: Any, claims: Any, question: str, history: Any, *, model: Any = None,
                     about_caller: Any = None, can_book: bool = False) -> str | None:
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
    assert service.spoken_text(await _say(env, call_id, "How long would the call be?")) == (
        "It's a 30-minute call with our team.")
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
    for question in ("Is there a warranty on the ads?", "And a warranty on the landing page?"):
        assert script.MISS_LINE in service.spoken_text(await _say(env, call_id, question))
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
                     llm_config: Any = None) -> str | None:
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
