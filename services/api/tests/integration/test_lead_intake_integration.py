"""A10 lead intake on a REAL migrated Postgres, over HTTP: landing-page forms, Meta
notification e-mails forwarded from Gmail, the Meta Lead Ads webhook (Graph API
faked). Leads, contacts, consents, plans, the outbox and the queue are real."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from httpx import ASGITransport, AsyncClient

from api.agents import dispatch, memory
from api.agents.executors import TransientExecutorError
from api.agents.repository import AgentSettings, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.config import get_api_settings
from api.intake import meta
from api.intake.routes import sign_intake

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)

_JWT = "x" * 48
_APP_SECRET = uuid4().hex  # per-run Meta app value for signing the fake webhooks
_VERIFY = uuid4().hex
_ENV = {
    "DEPLOYMENT_MODE": "saas", "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379", "JWT_SECRET": _JWT,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api", "LOG_LEVEL": "WARNING", "COOKIE_SECURE": "false",
    "EMAIL_LINK_SECRET": "l" * 40, "PUBLIC_API_BASE_URL": "https://api.example.com",
    "META_APP_SECRET": _APP_SECRET, "META_VERIFY_TOKEN": _VERIFY,
}
_CALL_WORDING = ("I agree that Acme Roofing may call me at this number, including with an AI "
                 "assistant and automated technology. Not a condition of purchase.")


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


class _Graph:
    def __init__(self) -> None:
        self.fail_next = 0
        self.fields = {"full_name": "Meta Person", "email": "meta@example.com",
                       "phone_number": "+14155550100"}

    async def fetch_lead(self, leadgen_id: str, page_token: str) -> dict[str, str]:
        if self.fail_next:
            self.fail_next -= 1
            raise TransientExecutorError("Meta HTTP 503")
        return {**self.fields, "form": "form-77"}


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
        "TRUNCATE lead_intake_events, tenant_lead_intake_configs, meta_page_routes, agent_events, "
        "outreach_tasks, outreach_plans, outreach_campaigns, agent_tool_calls, agent_runs, "
        "contact_consents, suppression_list, agent_settings, audit_chain_heads, audit_events")
    await db.execute("UPDATE platform_flags SET value = 'false' WHERE key = 'outreach_paused'")
    monkeypatch.setattr("api.leads.routes.sync_lead", MagicMock())
    monkeypatch.setattr("api.leads.routes.classify_lead_email", MagicMock())
    graph = _Graph()
    monkeypatch.setattr(meta, "graph_client", graph)
    tenants = []
    for _ in range(2):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, $1, $1, true, $1)", tid)
        await upsert_agent_settings(db, _claims(tid), AgentSettings(
            enabled_channels=("call", "email"), inbound_auto_enabled=True,
            physical_address="1 Main St"))
        tenants.append(tid)
    from api.app import create_app

    app = create_app()
    app.state.db, app.state.redis, app.state.cache = db, _Redis(), InMemoryCache()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"db": db, "client": client, "graph": graph, "a": tenants[0], "b": tenants[1]}
    await db.close()
    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims(tenant: str) -> AuthClaims:
    return AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)


def _cookie(tenant: str, role: Role = Role.CLIENT_ADMIN) -> dict[str, str]:
    token, _ = create_access_token(AuthClaims(subject="u", role=role, tenant_id=tenant),
                                   secret=_JWT, ttl_seconds=300)
    return {"access_token": token}


async def _form(env: dict[str, Any], tenant: str, **fields: str) -> Any:
    return await env["client"].post(f"/public/intake/forms/{sign_intake(tenant, 'form')}",
                                    json=fields)


async def _config(env: dict[str, Any], tenant: str, **body: Any) -> Any:
    return await env["client"].put("/admin/intake/config", cookies=_cookie(tenant), json=body)


def _notification(*, sender: str = "notification@facebookmail.com",
                  msg_id: str | None = None, subject: str = "New lead: Spring check-ups",
                  body: str | None = None) -> str:
    text = body or ("You have a new lead!\n\nFull name: Jane Doe\nEmail: jane@example.com\n"
                    "Phone number: (202) 555-0123\nWhat service do you need?: Flat roof\n")
    return "\r\n".join([
        f"From: Facebook <{sender}>", "To: rebecca@acme.com", f"Subject: {subject}",
        f"Message-ID: {msg_id or f'<{uuid4().hex}@facebookmail.com>'}",
        "Content-Type: text/plain; charset=utf-8", "", text, ""])


async def _parse(env: dict[str, Any], tenant: str, raw: str, *, dkim: str) -> Any:
    return await env["client"].post(f"/public/intake/email/{sign_intake(tenant, 'email')}",
                                    data={"email": raw, "SPF": "pass", "dkim": dkim})


async def test_form_lead_gets_email_consent_a_plan_and_the_usual_lead_steps(
    env: dict[str, Any],
) -> None:
    db, a = env["db"], env["a"]
    r = await _form(env, a, name="Jane Doe", email="Jane@Example.com", phone="(202) 555-0123",
                    call_consent="yes", submission_id="s-1", form="Roof quote",
                    **{"What service do you need?": "Flat roof repair"})
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    await dispatch.dispatch_events(db, limit=10)

    lead = await db.fetchrow("SELECT lead_id, source, email FROM leads WHERE tenant_id = $1", a)
    assert lead is not None and (lead["source"], lead["email"]) == ("web_form", "jane@example.com")
    contact = await db.fetchrow("SELECT contact_id, timezone FROM contacts WHERE lead_id = $1",
                                lead["lead_id"])
    assert contact is not None and contact["timezone"] == "America/New_York"  # from 202
    consents = await db.fetch("SELECT channel, basis, source FROM contact_consents "
                              "WHERE contact_id = $1", contact["contact_id"])
    # A phone + "yes" without the client's saved call wording is NOT call consent (I3).
    assert [tuple(c) for c in consents] == [("email", "express_consent", "form")]
    channels = [r["channel"] for r in await db.fetch(
        "SELECT channel FROM outreach_tasks WHERE tenant_id = $1 ORDER BY step_index", a)]
    assert channels and set(channels) == {"email"}
    answers = await db.fetchval("SELECT payload FROM lead_activities WHERE lead_id = $1 "
                                "AND type = 'intake_form_answers'", lead["lead_id"])
    assert answers["answers"] == {"What service do you need?": "Flat roof repair"}
    remembered = await memory.items(db, _claims(a), contact["contact_id"])  # A11 M4
    assert [i["text"] for i in remembered] == ["What service do you need?: Flat roof repair"]
    assert await db.fetchval("SELECT count(*) FROM notification_events WHERE tenant_id = $1 "
                             "AND kind = 'lead_captured'", a) == 1


async def test_call_consent_needs_the_clients_saved_wording(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    assert (await _config(env, a, call_consent_wording=_CALL_WORDING)).status_code == 200
    await _form(env, a, name="Ana Ruiz", email="ana@example.com", phone="(415) 555-0100",
                consent_to_call="on", submission_id="s-2")
    await dispatch.dispatch_events(db, limit=10)
    call = await db.fetchrow("SELECT basis, source, wording FROM contact_consents "
                             "WHERE tenant_id = $1 AND channel = 'call'", a)
    assert call is not None and tuple(call) == ("express_written_consent", "form", _CALL_WORDING)
    channels = {r["channel"] for r in await db.fetch(
        "SELECT channel FROM outreach_tasks WHERE tenant_id = $1", a)}
    assert channels == {"email", "call"}


async def test_duplicates_never_make_a_second_lead_or_plan(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    first = await _form(env, a, name="Jane Doe", email="jane@example.com", submission_id="s-3")
    again = await _form(env, a, name="Jane Doe", email="jane@example.com", submission_id="s-3")
    assert (first.json()["status"], again.json()["status"]) == ("accepted", "duplicate")
    await dispatch.dispatch_events(db, limit=10)
    other_form = await _form(env, a, email="JANE@example.com", submission_id="s-4",
                             form="Gutter quote")
    assert other_form.json()["status"] == "accepted"
    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT count(*) FROM leads WHERE tenant_id = $1", a) == 1
    assert await db.fetchval("SELECT count(*) FROM outreach_plans WHERE tenant_id = $1 "
                             "AND status = 'active'", a) == 1
    assert await db.fetchval("SELECT count(*) FROM lead_activities WHERE tenant_id = $1 "
                             "AND type = 'intake_form_answers'", a) == 2
    assert await db.fetchval("SELECT reason FROM lead_intake_events WHERE tenant_id = $1 "
                             "AND external_id = 's-4'", a) == "EXISTING_LEAD"


async def test_unusable_leads_go_to_the_queue_for_a_person(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], env["a"]
    r = await _form(env, a, name="No Contact", form="Roof quote")
    assert r.json() == {"status": "rejected"}
    assert await db.fetchval("SELECT count(*) FROM leads WHERE tenant_id = $1", a) == 0
    queue = (await client.get("/admin/intake/events", cookies=_cookie(a))).json()
    assert [(e["status"], e["reason"]) for e in queue] == [("rejected", "NO_CONTACT_DETAILS")]
    assert queue[0]["fields"]["values"] == {"name": "No Contact"}
    assert (await client.get("/admin/intake/events",
                             cookies=_cookie(a, Role.CLIENT_AGENT))).status_code == 403
    intake_id = queue[0]["intake_id"]
    assert (await client.post(f"/admin/intake/events/{intake_id}:resolve",
                              cookies=_cookie(a))).status_code == 200
    assert (await client.get("/admin/intake/events", cookies=_cookie(a))).json() == []


async def test_meta_notification_emails_forwarded_from_gmail(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], env["a"]
    msg_id = f"<{uuid4().hex}@facebookmail.com>"
    raw = _notification(msg_id=msg_id)
    ok = await _parse(env, a, raw, dkim="{@facebookmail.com : pass}")
    again = await _parse(env, a, raw, dkim="{@facebookmail.com : pass}")
    assert (ok.json()["status"], again.json()["status"]) == ("accepted", "duplicate")
    lead = await db.fetchrow("SELECT source, name, phone FROM leads WHERE tenant_id = $1", a)
    assert lead is not None and lead["source"] == "meta_email" and lead["name"] == "Jane Doe"

    forged = await _parse(env, a, _notification(), dkim="{@facebookmail.com : fail}")
    spoofed = await _parse(env, a, _notification(sender="leads@attacker.io"),
                           dkim="{@attacker.io : pass}")
    assert forged.json()["status"] == spoofed.json()["status"] == "rejected"
    assert await db.fetchval("SELECT count(*) FROM leads WHERE tenant_id = $1", a) == 1

    gmail = await _parse(env, a, _notification(
        sender="forwarding-noreply@google.com",
        subject="(#987654321) Gmail Forwarding Confirmation - Receive Mail from rebecca@acme.com",
        body="Please confirm."), dkim="{@google.com : pass}")
    assert gmail.json()["status"] == "notice"
    queue = (await client.get("/admin/intake/events", cookies=_cookie(a))).json()
    notice = next(e for e in queue if e["status"] == "notice")
    assert notice["fields"]["code"] == "987654321"
    assert {e["reason"] for e in queue} >= {"UNTRUSTED_SENDER", "GMAIL_FORWARDING_CONFIRMATION"}


def _signed(body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    digest = hmac.new(_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def _leadgen(page: str, leadgen: str) -> dict[str, Any]:
    return {"object": "page", "entry": [{"id": page, "changes": [{"field": "leadgen", "value": {
        "leadgen_id": leadgen, "page_id": page, "form_id": "form-77"}}]}]}


async def test_meta_lead_ads_webhook_fetches_with_retry(env: dict[str, Any]) -> None:
    db, client, graph, a, b = env["db"], env["client"], env["graph"], env["a"], env["b"]
    challenge = await client.get("/public/intake/meta", params={
        "hub.mode": "subscribe", "hub.verify_token": _VERIFY, "hub.challenge": "c-123"})
    assert challenge.status_code == 200 and challenge.text == "c-123"
    assert (await client.get("/public/intake/meta", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong",
        "hub.challenge": "x"})).status_code == 403

    assert (await _config(env, a, meta_page_id="111", meta_page_token=uuid4().hex)
            ).status_code == 200
    taken = await _config(env, b, meta_page_id="111")
    assert taken.status_code == 409 and taken.json()["error_code"] == "PAGE_IN_USE"

    raw, headers = _signed(_leadgen("111", "lg-1"))
    bad = await client.post("/public/intake/meta", content=raw, headers={
        **headers, "X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert bad.status_code == 401
    unknown_raw, unknown_headers = _signed(_leadgen("999", "lg-x"))
    assert (await client.post("/public/intake/meta", content=unknown_raw,
                              headers=unknown_headers)).json() == {"queued": 0}

    graph.fail_next = 1
    assert (await client.post("/public/intake/meta", content=raw, headers=headers)
            ).json() == {"queued": 1}
    assert (await client.post("/public/intake/meta", content=raw, headers=headers)
            ).json() == {"queued": 0}  # Meta redelivers: once
    assert (await dispatch.dispatch_events(db, limit=10))["retried"] == 1
    await db.execute("UPDATE agent_events SET available_at = now() WHERE status = 'pending'")
    await dispatch.dispatch_events(db, limit=10)
    lead = await db.fetchrow("SELECT source, email FROM leads WHERE tenant_id = $1", a)
    assert lead is not None and tuple(lead) == ("meta_api", "meta@example.com")
    assert await db.fetchval("SELECT status FROM lead_intake_events WHERE tenant_id = $1 "
                             "AND source = 'meta_api'", a) == "accepted"


async def test_tokens_are_tenant_bound(env: dict[str, Any]) -> None:
    db, client, a, b = env["db"], env["client"], env["a"], env["b"]
    await _form(env, b, email="bee@example.com", submission_id="s-b")
    assert await db.fetchval("SELECT count(*) FROM leads WHERE tenant_id = $1", a) == 0
    assert await db.fetchval("SELECT count(*) FROM leads WHERE tenant_id = $1", b) == 1
    form_as_email = sign_intake(a, "email")  # a token for another purpose is not a form token
    assert (await client.post(f"/public/intake/forms/{form_as_email}",
                              json={"email": "x@y.com"})).status_code == 404
    assert (await client.post("/public/intake/forms/forged.token",
                              json={"email": "x@y.com"})).status_code == 404
    config = (await client.get("/admin/intake/config", cookies=_cookie(a))).json()
    assert config["form_url"].startswith("https://api.example.com/public/intake/forms/")
    assert config["meta_webhook_url"] == "https://api.example.com/public/intake/meta"
    assert "meta_page_token" not in config
