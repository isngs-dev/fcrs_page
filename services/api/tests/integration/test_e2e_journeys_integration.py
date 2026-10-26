"""A9 H1/H2 end-to-end journeys on a REAL migrated Postgres.

Driven only the way production is: HTTP endpoints (widget, admin, SendGrid
Inbound Parse, signed Plivo webhooks), the outbox dispatcher and the task
dispatcher -- no internal shortcuts. Faked: the model (scripted by prompt type),
retrieval (one knowledge chunk), SendGrid and Plivo's REST API. Everything else
(gate, planner, composer, reply handling, call script, booking, audit) is real.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from httpx import ASGITransport, AsyncClient

from api.agents import dispatch, executors
from api.agents.consent_wording import CURRENT_VERSION
from api.agents.repository import AgentSettings, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.call_agent import repository as call_repo
from api.call_agent import service as call_service
from api.call_agent.executor import place_call
from api.call_agent.telephony import PlacedCall, Recording
from api.config import get_api_settings
from api.email_agent import composer, inbound_service, replies
from api.email_agent import service as email_service
from api.email_agent.config_repository import upsert_email_agent_config
from api.email_agent.executor import email_executor
from api.email_agent.links import sign_route
from api.email_agent.providers import OutboundEmail, SentRef
from api.gateway.sessions import mint_visitor_session
from api.llm.config_repository import LLMConfig
from api.llm.provider import Completion
from api.scheduling.repository import upsert_availability
from api.voice_agent import agent as voice_agent
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
_PLIVO_SIGNING = uuid4().hex  # per-run Plivo auth value for signing the fake webhooks
_ENV = {
    "DEPLOYMENT_MODE": "saas", "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379", "JWT_SECRET": _JWT,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api", "LOG_LEVEL": "WARNING", "COOKIE_SECURE": "false",
    "EMAIL_LINK_SECRET": "l" * 40, "PUBLIC_API_BASE_URL": _BASE,
}
_NY = ZoneInfo("America/New_York")
_KB = "We repair flat roofs and gutters. An inspection takes about an hour."


def _open_zone() -> str:
    now = datetime.now(UTC)
    for zone in ("America/New_York", "America/Chicago", "America/Los_Angeles", "Pacific/Honolulu",
                 "Europe/London", "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney",
                 "America/Sao_Paulo", "Europe/Moscow", "Asia/Dubai", "Pacific/Auckland"):
        if 10 <= now.astimezone(ZoneInfo(zone)).hour < 18:
            return zone
    raise AssertionError("no zone in the calling window")


class _Model:
    """Answers by prompt type, like a well-behaved model would."""

    def __init__(self) -> None:
        self.label = "book_meeting"
        self.proposed = ""
        self.prompts: list[Any] = []  # A14: proves answers are written in context

    async def generate(self, messages: Any, *, model: str, max_tokens: int,
                       reasoning_effort: str | None = None) -> Completion:
        self.prompts.append(messages)
        system = messages[0].content
        if "follow-up email" in system:
            text = ("SUBJECT: Your flat roof question\n\nHi Jane, thanks for asking about "
                    "flat-roof repairs. We repair flat roofs, and an inspection takes about an "
                    "hour. Would a short call to find a time suit you?")
        elif "meeting start time" in system:
            text = self.proposed
        else:
            text = "Yes, we repair flat roofs, and an inspection takes about an hour."
        return Completion(text=text, model=model, input_tokens=10, output_tokens=10)

    async def classify(self, text: str, labels: Any, *, model: str,
                       label_descriptions: Any = None) -> str:
        return self.label

    async def aclose(self) -> None:
        return None


class _Mailer:
    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []

    async def send(self, email: OutboundEmail) -> SentRef:
        self.sent.append(email)
        return SentRef(provider="fake", provider_message_id=f"sg-{len(self.sent)}")


class _Plivo:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def place_call(self, **kw: Any) -> PlacedCall:
        self.names.append("place_call")
        return PlacedCall(request_uuid="req-e2e")

    async def start_recording(self, **kw: Any) -> Recording:
        self.names.append("start_recording")
        return Recording(recording_id="rec-e2e", url="https://media.plivo.com/rec-e2e.mp3")

    async def stop_recording(self, **kw: Any) -> None:
        self.names.append("stop_recording")

    async def delete_recording(self, **kw: Any) -> None:
        self.names.append("delete_recording")

    async def transfer_to_url(self, **kw: Any) -> None:
        self.names.append("transfer_to_url")

    async def fetch_recording(self, **kw: Any) -> bytes:
        return b""


class _Redis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        return None

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

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
        "TRUNCATE email_messages, email_threads, tenant_email_agent_configs, call_sessions, "
        "tenant_call_agent_configs, telephony_numbers, outreach_imports, agent_events, "
        "outreach_tasks, outreach_plans, outreach_campaigns, agent_tool_calls, agent_runs, "
        "contact_consents, suppression_list, agent_settings, usage_costs, audit_chain_heads, "
        "audit_events, notification_events, schedule_events CASCADE")
    await db.execute("UPDATE platform_flags SET value = 'false' WHERE key = 'outreach_paused'")

    model, mailer, plivo = _Model(), _Mailer(), _Plivo()
    llm_config = LLMConfig(provider="fake", model="m", api_key="k", embedding_model="e")

    async def config(db: Any, claims: Any) -> LLMConfig:
        return llm_config

    async def retrieve(db: Any, claims: Any, query: str, *, k: int) -> Any:
        return SimpleNamespace(chunks=[SimpleNamespace(chunk_id="c1", doc_id="d1", content=_KB)])

    async def mail_provider(db: Any, claims: Any, cfg: Any) -> _Mailer:
        return mailer

    for module in (composer, replies, voice_agent):
        monkeypatch.setattr(module, "get_llm_config", config)
        monkeypatch.setattr(module, "provider_for", lambda cfg: model)
        monkeypatch.setattr(module, "retrieve_hybrid", retrieve)
    monkeypatch.setattr(email_service, "provider_for_tenant", mail_provider)
    monkeypatch.setattr(inbound_service, "provider_for_tenant", mail_provider)
    monkeypatch.setattr(call_service, "telephony_factory", lambda auth_id, token: plivo)
    notify = MagicMock()
    monkeypatch.setattr(call_service, "send_notification", notify)
    monkeypatch.setattr("api.leads.routes.sync_lead", MagicMock())
    monkeypatch.setattr("api.leads.routes.classify_lead_email", MagicMock())
    executors.register("email", email_executor)
    executors.register("call", place_call)
    executors.register_capacity("call", call_service.has_capacity)

    tid = f"t-{uuid4().hex[:10]}"
    await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
    await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                     "VALUES ($1, $1, $1, true, $1)", tid)
    claims = AuthClaims(subject="owner", role=Role.CLIENT_ADMIN, tenant_id=tid)
    await upsert_email_agent_config(
        db, claims, provider="sendgrid", from_address="rebecca@mail.acme.com", from_name="Acme",
        reply_to="sales@acme.com", booking_url=None, enabled=True, api_key=uuid4().hex,
        reply_domain="reply.acme.com")
    await call_repo.upsert_call_config(db, claims, auth_token=_PLIVO_SIGNING, values={
        "enabled": True, "plivo_auth_id": "MAE2E000000000000000", "from_number": "+17865550000",
        "transfer_number": None, "company_name": "Acme Roofing", "voice": "Polly.Joanna-Neural",
        "voice_model": None, "voicemail_policy": "hangup", "voicemail_message": None,
        "max_call_seconds": 300, "max_concurrent_calls": 2, "recording_enabled": True})
    await upsert_availability(db, claims, timezone="America/New_York", rules={
        "slot_minutes": 30, "buffer_minutes": 0,
        "weekly_hours": {d: [["00:00", "23:30"]] for d in
                         ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}})

    from api.app import create_app

    app = create_app()
    app.state.db, app.state.redis, app.state.cache = db, _Redis(), InMemoryCache()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"db": db, "client": client, "claims": claims, "tenant": tid, "model": model,
               "mailer": mailer, "plivo": plivo, "notify": notify}
    executors.unregister("email")
    executors.unregister("call")
    await db.close()
    get_settings.cache_clear()
    get_api_settings.cache_clear()


async def _settle(env: dict[str, Any], now: datetime | None = None) -> None:
    """One Beat tick: the outbox, then due tasks, then the outbox again."""
    await dispatch.dispatch_events(env["db"], limit=50)
    await dispatch.dispatch_due_tasks(env["db"], limit=50, now=now)
    await dispatch.dispatch_events(env["db"], limit=50)


async def _plivo(env: dict[str, Any], call_id: str, action: str,
                 params: dict[str, str]) -> str:
    path = f"/public/call-agent/calls/{call_id}/{action}?ticket={call_service.ticket(call_id)}"
    nonce = uuid4().hex
    r = await env["client"].post(path, data=params, headers={
        "X-Plivo-Signature-V3": compute_signature(_BASE + path, params, nonce, _PLIVO_SIGNING),
        "X-Plivo-Signature-V3-Nonce": nonce})
    assert r.status_code == 200, r.text
    return str(r.text)


async def test_h1_inbound_chat_lead_to_email_to_reply_to_booked(env: dict[str, Any]) -> None:
    db, client, mailer, tid = env["db"], env["client"], env["mailer"], env["tenant"]
    await upsert_agent_settings(db, env["claims"], AgentSettings(
        enabled_channels=("email",), email_mode="auto_send", inbound_auto_enabled=True,
        first_inbound_email_delay_minutes=0, physical_address="1 Main St, Miami FL"))

    # 1. A visitor leaves their details in the widget and ticks email follow-up.
    visitor, _ = mint_visitor_session(tid, secret=_JWT, ttl_seconds=600)
    r = await client.post("/public/leads", headers={"Authorization": f"Bearer {visitor}"}, json={
        "name": "Jane Doe", "email": "jane@example.com",
        "consent": {"granted": True, "purpose": "lead_followup", "text": "I agree."},
        "channel_consents": {"email": True, "call": False, "version": CURRENT_VERSION},
        "timezone": "America/New_York"})
    assert r.status_code == 201, r.text

    # 2. Rebecca plans and sends the first follow-up.
    await _settle(env, now=datetime.now(UTC) + timedelta(minutes=1))
    [first] = mailer.sent
    assert first.to == "jane@example.com" and first.subject == "Your flat roof question"
    assert "AI assistant" in first.body_text and "Unsubscribe" in first.body_text
    assert first.reply_to and first.reply_to.startswith("reply+")

    # 3. Jane replies with a time; SendGrid Inbound Parse posts it to us.
    day: date = (datetime.now(_NY) + timedelta(days=2)).date()
    env["model"].label, env["model"].proposed = "book_meeting", f"{day.isoformat()}T15:00"
    raw = "\r\n".join([
        "From: Jane Doe <jane@example.com>", f"To: {first.reply_to}",
        "Subject: Re: Your flat roof question", f"Message-ID: <{uuid4().hex}@example.com>",
        f"In-Reply-To: {first.message_id}", f"References: {first.message_id}",
        "Content-Type: text/plain; charset=utf-8", "",
        "Great, can we meet on that day at 3pm?", ""])
    r = await client.post(f"/public/email/inbound/{sign_route(tid)}", data={
        "email": raw, "SPF": "pass", "dkim": "{@example.com : pass}"})
    assert r.status_code == 200 and r.json() == {"stored": True}

    # 4. The reply is handled: booked, confirmed in the thread, follow-ups stopped.
    await _settle(env)
    await _settle(env)
    event = await db.fetchrow("SELECT starts_at, email FROM schedule_events WHERE tenant_id = $1",
                              tid)
    assert event is not None and event["email"] == "jane@example.com"
    assert event["starts_at"] == datetime(day.year, day.month, day.day, 15, 0,
                                          tzinfo=_NY).astimezone(UTC)
    confirmation = mailer.sent[-1]
    assert confirmation.to == "jane@example.com" and confirmation.in_reply_to
    assert await db.fetchval("SELECT status FROM outreach_plans WHERE tenant_id = $1",
                             tid) == "stopped"
    assert await db.fetchval("SELECT count(*) FROM outreach_tasks WHERE tenant_id = $1 "
                             "AND status IN ('scheduled', 'pending', 'in_progress')", tid) == 0
    assert len([e for e in mailer.sent if e.to == "jane@example.com"]) == 2
    assert await db.fetchval("SELECT count(*) FROM agent_events WHERE tenant_id = $1 "
                             "AND status <> 'done'", tid) == 0


async def test_h2_outbound_csv_to_ai_call_to_booked(env: dict[str, Any]) -> None:
    db, client, plivo, tid = env["db"], env["client"], env["plivo"], env["tenant"]
    await upsert_agent_settings(db, env["claims"], AgentSettings(
        enabled_channels=("call",), physical_address="1 Main St, Miami FL"))
    admin, _ = create_access_token(env["claims"], secret=_JWT, ttl_seconds=600)
    cookies = {"access_token": admin}

    # 1. An admin imports a list with written AI-call consent evidence and confirms it.
    consented = (date.today() - timedelta(days=10)).isoformat()
    csv = ("name,email,phone,timezone,call_consent,consent_date,consent_source\n"
           f"Ana Ruiz,ana@example.com,(202) 555-0199,{_open_zone()},yes,{consented},"
           "Signed web form\n")
    r = await client.post("/admin/outreach/imports", cookies=cookies,
                          files={"file": ("list.csv", csv.encode(), "text/csv")},
                          data={"campaign_name": "Spring check-ups"})
    assert r.status_code == 201, r.text
    preview = r.json()
    assert preview["report"]["valid"] == 1 and preview["report"]["call_consent_rows"] == 1
    unattested = await client.post(f"/admin/outreach/imports/{preview['import_id']}:commit",
                                   cookies=cookies, json={"consent_attested": False})
    assert unattested.json()["error_code"] == "CONSENT_ATTESTATION_REQUIRED"
    r = await client.post(f"/admin/outreach/imports/{preview['import_id']}:commit",
                          cookies=cookies, json={"consent_attested": True})
    assert r.status_code == 200 and r.json()["report"]["committed"]["plan_planned"] == 1

    # 2. The dispatcher dials; Plivo calls our webhooks back.
    await _settle(env)
    assert plivo.names == ["place_call"], [dict(r) for r in await db.fetch(
        "SELECT channel, status, scheduled_at, last_error, attempts FROM outreach_tasks")]
    call_id = str(await db.fetchval("SELECT call_id FROM call_sessions WHERE tenant_id = $1",
                                    tid))
    hello = call_service.spoken_text(await _plivo(env, call_id, "answer", {"CallUUID": "u-1"}))
    assert hello.startswith("Hi Ana, this is Rebecca, an AI assistant calling on behalf of "
                            "Acme Roofing. This call is recorded for quality.")
    said = call_service.spoken_text(await _plivo(env, call_id, "turn", {
        "Speech": "Do you fix flat roofs?", "CallUUID": "u-1"}))
    assert "we repair flat roofs" in said
    assert "start_recording" in plivo.names  # after the disclosure was heard
    offer = call_service.spoken_text(await _plivo(env, call_id, "turn", {
        "Speech": "I'd like to book a time", "CallUUID": "u-1"}))
    assert "Eastern time" in offer
    await _plivo(env, call_id, "turn", {"Speech": "the first one", "CallUUID": "u-1"})
    booked = call_service.spoken_text(await _plivo(env, call_id, "turn", {
        "Speech": "Yes please", "CallUUID": "u-1"}))
    assert booked.startswith("You're booked for")
    await _plivo(env, call_id, "hangup", {"CallUUID": "u-1", "Duration": "120",
                                          "CallStatus": "completed"})

    # 3. Settled: booked, confirmation queued, task done, follow-ups stopped, cost recorded.
    await _settle(env)
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1",
                             tid) == 1
    assert env["notify"].delay.called  # the booking confirmation email is queued
    row = await db.fetchrow("SELECT status, outcome, duration_s FROM call_sessions "
                            "WHERE call_id = $1", call_id)
    assert row is not None and tuple(row) == ("completed", "booked", 120)
    assert await db.fetchval("SELECT status FROM outreach_tasks WHERE tenant_id = $1",
                             tid) == "completed"
    assert await db.fetchval("SELECT status FROM outreach_plans WHERE tenant_id = $1",
                             tid) == "stopped"
    assert await db.fetchval("SELECT count(*) FROM usage_costs WHERE tenant_id = $1 "
                             "AND kind = 'telephony'", tid) == 1
    assert await db.fetchval("SELECT count(*) FROM agent_events WHERE tenant_id = $1 "
                             "AND status <> 'done'", tid) == 0
