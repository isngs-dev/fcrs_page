"""A4 inbound automation against a REAL migrated Postgres, through the real
visitor routes: widget lead + channel consent -> outbox -> contact linked to the
lead -> canonical consent rows -> inbound_auto plan. Plus admission flags,
validation-before-storage, automation-off, idempotency, convert reuse, isolation.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from httpx import ASGITransport, AsyncClient

from api.agents import dispatch
from api.agents.consent_wording import CURRENT_VERSION, WORDING
from api.agents.repository import AgentSettings, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.config import get_api_settings
from api.gateway.repository import _hash_client_key
from api.gateway.sessions import mint_visitor_session

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)

_JWT = "x" * 48
_ENV = {
    "DEPLOYMENT_MODE": "saas", "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379", "JWT_SECRET": _JWT,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api", "LOG_LEVEL": "WARNING", "COOKIE_SECURE": "false",
}
_STORE = {"granted": True, "purpose": "lead_followup", "text": "I agree to be contacted."}
_BOXES = {"email": True, "call": True, "version": CURRENT_VERSION}


class _Redis:
    """Rate limiter + blacklist stand-in: counters always low, nothing revoked."""

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
        "TRUNCATE agent_events, outreach_tasks, outreach_plans, outreach_campaigns, "
        "agent_tool_calls, agent_runs, contact_consents, suppression_list, agent_settings, "
        "audit_chain_heads, audit_events")
    await db.execute("UPDATE platform_flags SET value = 'false' WHERE key = 'outreach_paused'")
    tenants = []
    for _ in range(2):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute(
            "INSERT INTO tenants (id, name, slug, enabled, client_account_id, client_key_hash, "
            "allowed_origins) VALUES ($1, $1, $1, true, $1, $2, $3)",
            tid, _hash_client_key(f"pk_{tid}"), ["https://client.example"])
        tenants.append(tid)

    # Fire-and-forget Celery enqueues in the capture route: no broker in tests.
    from unittest.mock import MagicMock

    monkeypatch.setattr("api.leads.routes.sync_lead", MagicMock())
    monkeypatch.setattr("api.leads.routes.classify_lead_email", MagicMock())
    from api.app import create_app

    app = create_app()
    app.state.db, app.state.redis, app.state.cache = db, _Redis(), InMemoryCache()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"db": db, "client": client, "a": tenants[0], "b": tenants[1]}
    await db.close()
    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _visitor(tenant_id: str) -> dict[str, str]:
    token, _ = mint_visitor_session(tenant_id, secret=_JWT, ttl_seconds=600)
    return {"Authorization": f"Bearer {token}"}


async def _automation(db: Database, tenant_id: str, *, on: bool = True,
                      channels: tuple[str, ...] = ("call", "email")) -> None:
    claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)
    await upsert_agent_settings(db, claims, AgentSettings(
        enabled_channels=channels, inbound_auto_enabled=on, physical_address="1 Main St"))


async def _lead(env: dict[str, Any], tenant: str, **extra: Any) -> Any:
    body = {"name": "Jane Doe", "email": "jane@example.com", "phone": "(786) 823-3553",
            "consent": _STORE, **extra}
    return await env["client"].post("/public/leads", json=body, headers=_visitor(tenant))


# -- Admission flags --------------------------------------------------------------------------


async def test_admission_offers_only_usable_boxes(env: dict[str, Any]) -> None:
    db, client, a = env["db"], env["client"], env["a"]

    async def admit() -> dict[str, Any]:
        r = await client.post("/widget/session", json={"client_key": f"pk_{a}"},
                              headers={"Origin": "https://client.example"})
        assert r.status_code == 200, r.text
        return dict(r.json())

    assert "outreach_consent" not in await admit()  # no settings row
    await _automation(db, a, on=False)
    assert "outreach_consent" not in await admit()  # automation off
    await _automation(db, a, channels=("email",))
    assert (await admit())["outreach_consent"] == {
        "email": True, "call": False, "version": CURRENT_VERSION}


# -- Capture -> plan --------------------------------------------------------------------------


async def test_lead_with_consent_becomes_a_planned_contact(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    await _automation(db, a)
    r = await _lead(env, a, channel_consents=_BOXES, timezone="America/Chicago")
    assert r.status_code == 201
    lead_id = r.json()["lead_id"]
    assert await dispatch.dispatch_events(db, limit=10) == {
        "claimed": 1, "done": 1, "retried": 0, "dead": 0}

    contact = await db.fetchrow("SELECT contact_id, lead_id, timezone FROM contacts "
                                "WHERE tenant_id = $1", a)
    assert contact is not None
    assert (contact["lead_id"], contact["timezone"]) == (lead_id, "America/Chicago")
    identities = {r["identity_type"]: r["identity_value"] for r in await db.fetch(
        "SELECT identity_type, identity_value FROM contact_identities WHERE contact_id = $1",
        contact["contact_id"])}
    assert identities["email"] == "jane@example.com"
    assert identities["phone"] == "+17868233553"
    assert "visitor_id" in identities

    consents = {r["channel"]: r for r in await db.fetch(
        "SELECT channel, basis, source, wording, wording_version, evidence "
        "FROM contact_consents WHERE contact_id = $1", contact["contact_id"])}
    assert consents["call"]["basis"] == "express_written_consent"
    assert consents["call"]["wording"] == WORDING[CURRENT_VERSION]["call"]
    assert consents["email"]["source"] == "widget"
    assert consents["call"]["evidence"]["lead_id"] == lead_id

    tasks = await db.fetch("SELECT channel, scheduled_at FROM outreach_tasks ORDER BY step_index")
    assert [t["channel"] for t in tasks] == ["email", "call", "email"]
    first = tasks[0]["scheduled_at"] - datetime.now(UTC)
    assert timedelta(minutes=3) < first <= timedelta(minutes=5)  # inbound: email +5 min
    assert await db.fetchval("SELECT lead_id FROM leads WHERE lead_id = $1", lead_id)
    assert await db.fetchval("SELECT converted_to_contact_id FROM leads "
                             "WHERE lead_id = $1", lead_id) is None  # not converted (H5)


async def test_bad_consent_is_rejected_before_anything_is_stored(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    await _automation(db, a)
    r = await _lead(env, a, channel_consents={**_BOXES, "version": "1999-x"})
    assert r.status_code == 422 and r.json()["error_code"] == "CONSENT_VERSION_UNKNOWN"
    r = await _lead(env, a, phone=None, channel_consents=_BOXES)
    assert r.status_code == 422 and r.json()["error_code"] == "CALL_CONSENT_NEEDS_PHONE"
    r = await env["client"].post("/public/chat/identity", headers=_visitor(a), json={
        "name": "Jane", "email": "jane@example.com", "consent": _STORE,
        "channel_consents": {"email": True, "call": True, "version": CURRENT_VERSION}})
    assert r.status_code == 422 and r.json()["error_code"] == "CALL_CONSENT_NEEDS_PHONE"
    assert await db.fetchval("SELECT count(*) FROM leads WHERE tenant_id = $1", a) == 0
    assert await db.fetchval("SELECT count(*) FROM agent_events") == 0


async def test_nothing_happens_without_boxes_or_with_automation_off(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    await _automation(db, a, on=False)
    await _lead(env, a)  # no boxes -> no event at all
    assert await db.fetchval("SELECT count(*) FROM agent_events") == 0
    await _lead(env, a, channel_consents={**_BOXES, "call": False})
    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT last_error FROM agent_events") == "AUTOMATION_OFF"
    assert await db.fetchval("SELECT count(*) FROM contacts WHERE tenant_id = $1", a) == 0


async def test_identity_form_email_consent_and_one_plan_per_contact(env: dict[str, Any]) -> None:
    db, a, client = env["db"], env["a"], env["client"]
    await _automation(db, a)
    visitor = _visitor(a)
    r = await client.post("/public/chat/identity", headers=visitor, json={
        "name": "Jane", "email": "jane@example.com", "consent": _STORE,
        "channel_consents": {"email": True, "call": False, "version": CURRENT_VERSION}})
    assert r.status_code == 201
    r = await client.post("/public/leads", headers=visitor, json={
        "name": "Jane", "email": "jane@example.com", "consent": _STORE,
        "channel_consents": {"email": True, "call": False, "version": CURRENT_VERSION}})
    assert r.status_code == 201
    await dispatch.dispatch_events(db, limit=10)
    notes = sorted(str(r["last_error"]) for r in await db.fetch("SELECT last_error FROM agent_events"))
    assert notes == ["PLAN_ALREADY_ACTIVE", "planned"]
    assert await db.fetchval("SELECT count(*) FROM contacts WHERE tenant_id = $1", a) == 1
    assert await db.fetchval("SELECT count(*) FROM outreach_plans") == 1
    channels = [r["channel"] for r in await db.fetch("SELECT channel FROM outreach_tasks")]
    assert "call" not in channels  # no call consent -> policy drops it


async def test_convert_reuses_the_linked_contact(env: dict[str, Any]) -> None:
    db, a, client = env["db"], env["a"], env["client"]
    await _automation(db, a)
    lead_id = (await _lead(env, a, channel_consents=_BOXES)).json()["lead_id"]
    await dispatch.dispatch_events(db, limit=10)
    linked = await db.fetchval("SELECT contact_id FROM contacts WHERE lead_id = $1", lead_id)
    token, _ = create_access_token(AuthClaims(subject="admin-a", role=Role.CLIENT_ADMIN,
                                              tenant_id=a), secret=_JWT, ttl_seconds=300)
    r = await client.post(f"/admin/leads/{lead_id}/convert", json={},
                          cookies={"access_token": token})
    assert r.status_code in (200, 201), r.text
    assert r.json()["contact_id"] == linked
    assert await db.fetchval("SELECT count(*) FROM contacts WHERE tenant_id = $1", a) == 1


async def test_tenant_b_automation_is_independent(env: dict[str, Any]) -> None:
    db, a, b = env["db"], env["a"], env["b"]
    await _automation(db, a)  # only A automates
    await _lead(env, b, channel_consents=_BOXES)
    await _lead(env, a, channel_consents=_BOXES)
    await dispatch.dispatch_events(db, limit=10)
    rows = await db.fetch("SELECT tenant_id, last_error FROM agent_events ORDER BY tenant_id")
    assert {r["tenant_id"]: r["last_error"] for r in rows} == {a: "planned", b: "AUTOMATION_OFF"}
    assert await db.fetchval("SELECT count(*) FROM contacts WHERE tenant_id = $1", b) == 0
    assert await db.fetchval("SELECT count(DISTINCT tenant_id) FROM outreach_plans") == 1


async def test_chat_captured_name_then_email_feeds_inbound_automation(env: dict[str, Any]) -> None:
    """Conversational capture: name first (no email), email later -> lead filled
    in, and the email counts as follow-up consent with the chat wording."""
    from api.orchestrator import identity_capture as ic

    db, a = env["db"], env["a"]
    await _automation(db, a)
    visitor = AuthClaims(subject=f"v-{uuid4().hex[:8]}", role=Role.VISITOR, tenant_id=a)

    lead = await ic.save_name(db, visitor, None, "Jane Doe")
    assert lead is not None and (lead.name, lead.email) == ("Jane Doe", None)
    assert await db.fetchval("SELECT count(*) FROM agent_events") == 0  # no email, no follow-up

    lead = await ic.save_email(db, visitor, lead, "jane@example.com")
    assert lead is not None and lead.email == "jane@example.com"
    assert await db.fetchval("SELECT count(*) FROM leads WHERE tenant_id = $1", a) == 1
    assert lead.consent["chat_name_captured_at"] and lead.consent["chat_email_captured_at"]

    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT last_error FROM agent_events") == "planned"
    consent = await db.fetchrow("SELECT channel, basis, wording, wording_version "
                                "FROM contact_consents")
    assert consent is not None and consent["channel"] == "email"
    assert consent["wording"] == WORDING["chat-2026-10-a"]["email"]
    assert [r["channel"] for r in await db.fetch("SELECT channel FROM outreach_tasks")] == [
        "email", "email"]  # no call consent -> no call step
