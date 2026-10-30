"""/admin/outreach RBAC + isolation over HTTP against a REAL migrated Postgres (A1.2/A1.4/A1.5).

Roles: CLIENT_ADMIN (all), CLIENT_AGENT (read + own contacts only; 404 for
others), VISITOR (403), PLATFORM_ADMIN (only via /admin/tenants/{id}/outreach
and the platform kill switch).
"""
from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token
from api.contacts.repository import add_identity, create_contact, set_contact_timezone
from api.email_agent import config_repository

_ADDRESS = "1 Main St, Miami FL 33101"


async def upsert_email_agent_config(claims: AuthClaims, *, db: Database) -> None:
    """A2: a tenant needs a sender before it may enable the email channel."""
    await config_repository.upsert_email_agent_config(
        db, claims, provider="sendgrid", from_address="rebecca@mail.acme.com", from_name="Acme",
        reply_to=None, booking_url=None, enabled=True, api_key="SG.test-key")

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
    # A2: enabling the email channel needs link signing + a public base URL.
    "EMAIL_LINK_SECRET": "l" * 40, "PUBLIC_API_BASE_URL": "https://api.example.com",
}


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
async def env() -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    assert _TEST_DB_URL is not None
    db = await Database.connect(_TEST_DB_URL, min_size=1, max_size=5, statement_cache_size=0)
    await db.execute("TRUNCATE agent_events, outreach_tasks, outreach_plans, outreach_campaigns, "
                     "agent_tool_calls, agent_runs, contact_consents, suppression_list, "
                     "agent_settings, audit_chain_heads, audit_events")
    await db.execute("UPDATE platform_flags SET value = 'false' WHERE key = 'outreach_paused'")
    tenant_a, tenant_b = f"t-{uuid4().hex[:10]}", f"t-{uuid4().hex[:10]}"
    for tid in (tenant_a, tenant_b):
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, $1, $1, true, $1)", tid)
    admin_a = AuthClaims(subject="admin-a", role=Role.CLIENT_ADMIN, tenant_id=tenant_a)
    owned = await create_contact(db, admin_a, account_id=None, lead_id=None, name="Own",
                                 email="own@x.com", phone="2025550100", consent={},
                                 owner_agent_id="agent-a")
    other = await create_contact(db, admin_a, account_id=None, lead_id=None, name="Other",
                                 email="other@x.com", phone="2025550101", consent={})
    for cid, email in ((owned, "own@x.com"), (other, "other@x.com")):
        await add_identity(db, admin_a, cid, identity_type="email", identity_value=email)
        await set_contact_timezone(db, admin_a, cid, "America/New_York")

    get_settings.cache_clear()
    get_api_settings.cache_clear()
    old = {k: os.environ.get(k) for k in _ENV}
    os.environ.update(_ENV)
    try:
        from api.app import create_app

        app = create_app()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    app.state.db, app.state.redis, app.state.cache = db, _Redis(), InMemoryCache()
    await db.execute("TRUNCATE tenant_email_agent_configs, tenant_call_agent_configs")
    await upsert_email_agent_config(admin_a, db=db)
    # A6: enabling the call channel needs a calling config too.
    from api.call_agent.repository import upsert_call_config  # noqa: PLC0415

    await upsert_call_config(db, admin_a, auth_token=uuid4().hex, values={
        "enabled": True, "plivo_auth_id": "MAXXXXXXXXXXXXXXXXXX", "from_number": "+17865550000",
        "transfer_number": None, "company_name": "Acme", "voice": "Polly.Joanna-Neural",
        "voice_model": None, "voicemail_policy": "hangup", "voicemail_message": None,
        "max_call_seconds": 300, "max_concurrent_calls": 2, "recording_enabled": True})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "db": db, "a": tenant_a, "b": tenant_b, "owned": owned,
               "other": other}
    await db.close()


def _cookie(role: Role, tenant_id: str | None, subject: str = "admin-a") -> dict[str, str]:
    token, _ = create_access_token(AuthClaims(subject=subject, role=role, tenant_id=tenant_id),
                                   secret=_JWT, ttl_seconds=300)
    return {"access_token": token}


async def test_rbac_matrix(env: dict[str, Any]) -> None:
    c, a = env["client"], env["a"]
    admin = _cookie(Role.CLIENT_ADMIN, a)
    agent = _cookie(Role.CLIENT_AGENT, a, subject="agent-a")
    visitor = _cookie(Role.VISITOR, a, subject="v-1")
    platform = _cookie(Role.PLATFORM_ADMIN, None, subject="root")
    body = {"enabled_channels": ["email", "call"], "physical_address": _ADDRESS}

    assert (await c.get("/admin/outreach/settings", cookies=agent)).status_code == 200
    assert (await c.put("/admin/outreach/settings", json=body, cookies=agent)).status_code == 403
    assert (await c.put("/admin/outreach/settings", json=body, cookies=visitor)).status_code == 403
    assert (await c.get("/admin/outreach/settings", cookies=visitor)).status_code == 403
    assert (await c.get("/admin/outreach/settings", cookies=platform)).status_code == 403
    r = await c.put("/admin/outreach/settings", json=body, cookies=admin)
    assert r.status_code == 200 and r.json()["enabled_channels"] == ["call", "email"]
    r = await c.put("/admin/outreach/settings", json={**body, "region_profile": "UK"},
                    cookies=admin)
    assert r.status_code == 422 and r.json()["error_code"] == "REGION_UNSUPPORTED"
    r = await c.put("/admin/outreach/settings",
                    json={**body, "call_window_start": "21:00", "call_window_end": "09:00"},
                    cookies=admin)
    assert r.status_code == 422

    assert (await c.post("/admin/outreach/pause", cookies=agent)).status_code == 403
    assert (await c.post("/admin/outreach/pause", cookies=admin)).json() == {"outreach_paused": True}
    await c.post("/admin/outreach/resume", cookies=admin)

    # PLATFORM_ADMIN reaches tenant A only through the tenant-scoped router.
    r = await c.get(f"/admin/tenants/{a}/outreach/settings", cookies=platform)
    assert r.status_code == 200 and r.json()["enabled_channels"] == ["call", "email"]
    assert (await c.get(f"/admin/tenants/{a}/outreach/settings", cookies=admin)).status_code == 403

    assert (await c.put("/admin/outreach/platform/pause", json={"paused": True},
                        cookies=admin)).status_code == 403
    r = await c.put("/admin/outreach/platform/pause", json={"paused": True}, cookies=platform)
    assert r.status_code == 200
    gate = await c.get(f"/admin/outreach/contacts/{env['owned']}/gate?channel=email",
                       cookies=admin)
    assert gate.json()["code"] == "OUTREACH_PAUSED_PLATFORM"
    await c.put("/admin/outreach/platform/pause", json={"paused": False}, cookies=platform)
    assert await env["db"].fetchval(
        "SELECT count(*) FROM audit_events WHERE tenant_id = '__platform__'") == 2


async def test_agent_sees_only_own_contacts_and_suppression_delete_is_admin_only(
    env: dict[str, Any],
) -> None:
    c, a = env["client"], env["a"]
    admin = _cookie(Role.CLIENT_ADMIN, a)
    agent = _cookie(Role.CLIENT_AGENT, a, subject="agent-a")
    consent = {"channel": "email", "status": "granted", "basis": "documented_lawful_basis"}

    r = await c.post(f"/admin/outreach/contacts/{env['owned']}/consents", json=consent,
                     cookies=agent)
    assert r.status_code == 201
    r = await c.post(f"/admin/outreach/contacts/{env['other']}/consents", json=consent,
                     cookies=agent)
    assert r.status_code == 404
    assert (await c.get(f"/admin/outreach/contacts/{env['other']}/gate?channel=email",
                        cookies=agent)).status_code == 404
    r = await c.post(f"/admin/outreach/contacts/{env['owned']}/consents",
                     json={**consent, "source": "csv"}, cookies=agent)
    assert r.status_code == 422  # only admin_form via this API

    r = await c.post("/admin/outreach/suppressions",
                     json={"channel": "email", "value": "Own@X.com"}, cookies=agent)
    assert r.status_code == 201 and r.json()["value"] == "own@x.com"
    again = await c.post("/admin/outreach/suppressions",
                         json={"channel": "email", "value": "own@x.com"}, cookies=agent)
    assert again.status_code == 200 and again.json()["suppression_id"] == r.json()["suppression_id"]
    sid = r.json()["suppression_id"]
    assert (await c.delete(f"/admin/outreach/suppressions/{sid}", cookies=agent)).status_code == 403
    assert (await c.delete(f"/admin/outreach/suppressions/{sid}", cookies=admin)).status_code == 204


async def test_tenant_b_cannot_touch_tenant_a_contacts(env: dict[str, Any]) -> None:
    c, b = env["client"], env["b"]
    admin_b = _cookie(Role.CLIENT_ADMIN, b, subject="admin-b")
    consent = {"channel": "email", "status": "granted", "basis": "documented_lawful_basis"}
    owned = env["owned"]
    assert (await c.post(f"/admin/outreach/contacts/{owned}/consents", json=consent,
                         cookies=admin_b)).status_code == 404
    assert (await c.get(f"/admin/outreach/contacts/{owned}/consents",
                        cookies=admin_b)).status_code == 404
    assert (await c.post(f"/admin/outreach/contacts/{owned}/plans", json={},
                         cookies=admin_b)).status_code == 404
    assert (await c.get(f"/admin/outreach/contacts/{owned}/plan",
                        cookies=admin_b)).status_code == 404


async def test_recipe_plan_flow_and_audit_verify(env: dict[str, Any]) -> None:
    """The A1 manual recipe, end to end over HTTP (dispatcher run in-process)."""
    from api.agents.dispatch import dispatch_events
    from api.audit.chain import seal_audit_chain

    c, a, db, contact = env["client"], env["a"], env["db"], env["owned"]
    admin = _cookie(Role.CLIENT_ADMIN, a)
    r = await c.put("/admin/outreach/settings", cookies=admin,
                    json={"enabled_channels": ["email", "call"], "physical_address": _ADDRESS,
                          # A12: an all-day e-mail window so the check isn't clock-bound
                          "email_window_start": "00:00", "email_window_end": "23:59"})
    assert r.status_code == 200
    await c.post(f"/admin/outreach/contacts/{contact}/consents", cookies=admin, json={
        "channel": "email", "status": "granted", "basis": "documented_lawful_basis",
        "wording": "Existing customer"})
    assert (await c.get(f"/admin/outreach/contacts/{contact}/gate?channel=email",
                        cookies=admin)).json()["ok"] is True
    call_gate = await c.get(f"/admin/outreach/contacts/{contact}/gate?channel=call",
                            cookies=admin)
    assert call_gate.json()["code"] == "NO_CONSENT"

    r = await c.post(f"/admin/outreach/contacts/{contact}/plans",
                     json={"kind": "outbound_manual", "goal": "follow_up"}, cookies=admin)
    assert r.status_code == 202
    event_id = r.json()["event_id"]
    await dispatch_events(db, limit=10)
    status = await c.get(f"/admin/outreach/events/{event_id}", cookies=admin)
    assert status.json()["status"] == "done"
    plan = (await c.get(f"/admin/outreach/contacts/{contact}/plan", cookies=admin)).json()
    assert [t["channel"] for t in plan["tasks"]] == ["email", "email"]
    assert {t["status"] for t in plan["tasks"]} == {"scheduled"}
    assert "NO_CONSENT" in [n["code"] for n in plan["policy_notes"]]

    again = await c.post(f"/admin/outreach/contacts/{contact}/plans", json={}, cookies=admin)
    assert again.status_code == 409 and again.json()["error_code"] == "PLAN_ALREADY_ACTIVE"

    r = await c.post(f"/admin/outreach/plans/{plan['plan_id']}:cancel", cookies=admin)
    assert r.json() == {"plan_id": plan["plan_id"], "tasks_cancelled": 2}

    await seal_audit_chain(db)
    verify = (await c.get("/admin/audit/verify", cookies=admin)).json()
    assert verify["ok"] is True and verify["rows_checked"] >= 4 and verify["unsealed"] == 0
    platform = _cookie(Role.PLATFORM_ADMIN, None, subject="root")
    r = await c.get(f"/admin/tenants/{a}/audit/verify", cookies=platform)
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_the_ask_for_details_switch_lives_in_outreach_settings(env: dict[str, Any]) -> None:
    """The chatbot's name/email/number capture is set next to the channels it feeds (moved
    from /admin/settings, 2026-10-30): a save without it changes nothing, and the rest of the
    orchestrator config survives the full-row write."""
    from api.orchestrator.config_repository import (  # noqa: PLC0415
        get_orchestrator_config,
        upsert_orchestrator_config,
    )

    c, db, a, b = env["client"], env["db"], env["a"], env["b"]
    admin, other_admin = _cookie(Role.CLIENT_ADMIN, a), _cookie(Role.CLIENT_ADMIN, b)
    agent = _cookie(Role.CLIENT_AGENT, a, subject="agent-a")
    platform = _cookie(Role.PLATFORM_ADMIN, None, subject="root")
    claims = AuthClaims(subject="admin-a", role=Role.CLIENT_ADMIN, tenant_id=a)
    await upsert_orchestrator_config(db, claims, answer_threshold=0.8, escalate_threshold=0.3,
                                     turn_cap=9)

    first = await c.get("/admin/outreach/settings", cookies=admin)
    assert first.json()["identity_gate_enabled"] is False
    on = await c.put("/admin/outreach/settings", json={"identity_gate_enabled": True},
                     cookies=admin)
    assert on.status_code == 200 and on.json()["identity_gate_enabled"] is True
    config = await get_orchestrator_config(db, claims)
    assert (config.identity_gate_enabled, config.turn_cap) == (True, 9)
    assert (config.answer_threshold, config.escalate_threshold) == (0.8, 0.3)

    kept = await c.put("/admin/outreach/settings", json={}, cookies=admin)
    assert kept.json()["identity_gate_enabled"] is True

    r = await c.put("/admin/outreach/settings", json={"identity_gate_enabled": False},
                    cookies=agent)
    assert r.status_code == 403
    assert (await get_orchestrator_config(db, claims)).identity_gate_enabled is True

    # Tenant isolation: A's switch isn't B's.
    r = await c.get("/admin/outreach/settings", cookies=other_admin)
    assert r.json()["identity_gate_enabled"] is False

    # PLATFORM_ADMIN sets it only through the tenant-scoped router.
    r = await c.put(f"/admin/tenants/{a}/outreach/settings",
                    json={"identity_gate_enabled": False}, cookies=platform)
    assert r.status_code == 200 and r.json()["identity_gate_enabled"] is False
    assert (await get_orchestrator_config(db, claims)).identity_gate_enabled is False
