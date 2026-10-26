"""A2 Email Agent against a REAL migrated Postgres: executor (auto-send + draft),
approval with the gate re-run, discard, draft expiry, transient retry with a
stable Message-ID, threading, unsubscribe, booking hook, routes RBAC + isolation.

The LLM composer and the mail provider are faked (no network); everything
else -- SQL, gate, dispatcher, audit, outbox -- is real.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from common.errors import ConflictError, ValidationError
from httpx import ASGITransport, AsyncClient

from api.agents import dispatch, executors, hooks, planner
from api.agents.executors import TransientExecutorError
from api.agents.repository import AgentSettings, add_consent, add_suppression, upsert_agent_settings
from api.auth.tokens import create_access_token
from api.config import get_api_settings
from api.contacts.repository import add_identity, create_contact, set_contact_timezone
from api.email_agent import repository as repo
from api.email_agent import service
from api.email_agent.composer import Composed
from api.email_agent.config_repository import upsert_email_agent_config
from api.email_agent.executor import email_executor
from api.email_agent.links import sign_unsubscribe
from api.email_agent.providers import OutboundEmail, SentRef
from api.leads.repository import create_lead

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
    "EMAIL_LINK_SECRET": "l" * 40, "PUBLIC_API_BASE_URL": "https://api.example.com",
}
_ADDRESS = "1 Main St, Miami FL 33101"


class _Mailer:
    """Records every send; can fail the next N sends transiently."""

    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []
        self.fail_next = 0

    async def send(self, email: OutboundEmail) -> SentRef:
        if self.fail_next:
            self.fail_next -= 1
            raise TransientExecutorError("SendGrid HTTP 503")
        self.sent.append(email)
        return SentRef(provider="fake", provider_message_id=f"pm-{len(self.sent)}")


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
        "TRUNCATE email_messages, email_threads, tenant_email_agent_configs, agent_events, "
        "outreach_tasks, outreach_plans, outreach_campaigns, agent_tool_calls, agent_runs, "
        "contact_consents, suppression_list, agent_settings, usage_costs, audit_chain_heads, "
        "audit_events")
    await db.execute("UPDATE platform_flags SET value = 'false' WHERE key = 'outreach_paused'")

    mailer = _Mailer()

    async def fake_provider(db: Any, claims: Any, config: Any) -> _Mailer:
        return mailer

    async def fake_compose(db: Any, claims: Any, **kw: Any) -> Composed:
        return Composed(band="answer", subject="Your roof enquiry",
                        body=f"Hi {kw['first_name']}, following up as promised.",
                        sources=[{"chunk_id": "c1", "doc_id": "d1"}])

    monkeypatch.setattr(service, "provider_for_tenant", fake_provider)
    monkeypatch.setattr(service, "compose", fake_compose)
    executors.register("email", email_executor)

    tenants = []
    for _ in range(2):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, $1, $1, true, $1)", tid)
        tenants.append(tid)
    yield {"db": db, "mailer": mailer, "a": tenants[0], "b": tenants[1]}
    executors.unregister("email")
    await db.close()
    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims(tenant_id: str, role: Role = Role.CLIENT_ADMIN, subject: str = "admin-a"
            ) -> AuthClaims:
    return AuthClaims(subject=subject, role=role, tenant_id=tenant_id)


async def _setup(db: Database, claims: AuthClaims, *, mode: str = "auto_send",
                 email: str = "Jane@Example.com", lead: bool = False,
                 owner: str | None = None) -> str:
    await upsert_email_agent_config(db, claims, provider="sendgrid",
                                    from_address="rebecca@mail.acme.com", from_name="Acme",
                                    reply_to="sales@acme.com", booking_url="https://cal.com/acme",
                                    enabled=True, api_key="SG.test-key")
    await upsert_agent_settings(db, claims, AgentSettings(
        enabled_channels=("email",), email_mode=mode, physical_address=_ADDRESS,
        email_window_start=time(0), email_window_end=time(23, 59)))  # A12: not clock-bound
    lead_id = None
    if lead:
        lead_id = await create_lead(db, claims, visitor_id=f"v-{uuid4().hex[:6]}", name="Jane Doe",
                                    email=email, phone=None, consent={"granted": True},
                                    source="widget")
    contact_id = await create_contact(db, claims, account_id=None, lead_id=lead_id,
                                      name="Jane Doe", email=email, phone=None,
                                      consent={"granted": True}, owner_agent_id=owner)
    await add_identity(db, claims, contact_id, identity_type="email", identity_value=email)
    await set_contact_timezone(db, claims, contact_id, "America/New_York")
    await add_consent(db, claims, contact_id, channel="email", status="granted",
                      basis="documented_lawful_basis", source="admin_form")
    return contact_id


async def _plan_and_dispatch(db: Database, claims: AuthClaims, contact_id: str,
                             now: datetime) -> dict[str, int]:
    outcome = await planner.create_plan(db, claims, contact_id=contact_id,
                                        kind="outbound_manual", goal=None, now=now)
    assert outcome.plan_id
    return await dispatch.dispatch_due_tasks(db, limit=10, now=now)


async def _task(db: Database, step: int = 0) -> Any:
    return await db.fetchrow("SELECT task_id, status, attempts, last_error, result "
                             "FROM outreach_tasks WHERE step_index = $1", step)


# -- Executor ----------------------------------------------------------------------------


async def test_auto_send_sends_with_headers_and_writes_back(env: dict[str, Any]) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a, lead=True)
    now = datetime.now(UTC)
    counts = await _plan_and_dispatch(db, a, contact_id, now)
    assert counts.get("completed") == 1

    [email] = mailer.sent
    assert email.to == "jane@example.com" and email.reply_to == "sales@acme.com"
    assert email.message_id.endswith("@mail.acme.com>") and email.in_reply_to is None
    assert email.unsubscribe_url and "/public/email/unsubscribe/" in email.unsubscribe_url
    assert "Book a time that suits you: https://cal.com/acme" in email.body_text
    assert "— Rebecca, AI assistant for Acme" in email.body_text and _ADDRESS in email.body_text

    task = await _task(db)
    assert task["status"] == "completed"
    message = await repo.get_message_for_task(db, a, task["task_id"])
    assert message is not None and message.status == "sent"
    assert message.provider_message_id == "pm-1"
    assert await db.fetchval("SELECT count(*) FROM usage_costs WHERE kind = 'email'") == 1
    assert await db.fetchval("SELECT count(*) FROM lead_activities WHERE tenant_id = $1 "
                             "AND type = 'agent_email_sent'", env["a"]) == 1


async def test_draft_mode_parks_task_until_approved(env: dict[str, Any]) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a, mode="draft_for_review")
    now = datetime.now(UTC)
    await _plan_and_dispatch(db, a, contact_id, now)
    assert mailer.sent == []
    task = await _task(db)
    assert task["status"] == "pending"
    # A parked task is neither claimed again nor reaped.
    assert (await dispatch.dispatch_due_tasks(db, limit=10, now=now))["claimed"] == 0
    assert (await dispatch.reap(db, lease_seconds=0))["tasks"] == 0

    message = await repo.get_message_for_task(db, a, task["task_id"])
    assert message is not None and message.status == "draft"
    result = await service.approve(db, a, message.message_id)
    assert result["status"] == "sent" and len(mailer.sent) == 1
    assert (await _task(db))["status"] == "completed"
    approved = await repo.get_message(db, a, message.message_id)
    assert approved is not None and approved.approved_by == "admin-a"

    with pytest.raises(ConflictError):  # can't approve twice
        await service.approve(db, a, message.message_id)


async def test_approve_with_a_broken_sender_fails_the_email_not_leaves_it_approved(
    env: dict[str, Any],
) -> None:
    """A8 walkthrough bug: a config error before the provider is built left the
    message 'approved' forever (never sent, never retryable)."""
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a, mode="draft_for_review")
    await _plan_and_dispatch(db, a, contact_id, datetime.now(UTC))
    message = await repo.get_message_for_task(db, a, (await _task(db))["task_id"])
    assert message is not None
    await db.execute("UPDATE tenant_email_agent_configs SET enabled = false")
    with pytest.raises(ValidationError):
        await service.approve(db, a, message.message_id)
    after = await repo.get_message(db, a, message.message_id)
    assert after is not None and after.status == "failed" and after.last_error
    assert (await _task(db))["status"] == "failed"
    assert mailer.sent == []


async def test_approve_reruns_the_gate(env: dict[str, Any]) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a, mode="draft_for_review")
    await _plan_and_dispatch(db, a, contact_id, datetime.now(UTC))
    message = await repo.get_message_for_task(db, a, (await _task(db))["task_id"])
    assert message is not None
    await add_suppression(db, a, channel="email", value="jane@example.com", reason="manual",
                          source="test")
    with pytest.raises(ConflictError) as exc:
        await service.approve(db, a, message.message_id)
    assert exc.value.code == "CONTACT_SUPPRESSED"
    assert mailer.sent == []
    after = await repo.get_message(db, a, message.message_id)
    assert after is not None and after.status == "discarded"
    assert (await _task(db))["status"] == "cancelled"


async def test_discard_and_draft_expiry_cancel_the_task(env: dict[str, Any]) -> None:
    db, a = env["db"], _claims(env["a"])
    c1 = await _setup(db, a, mode="draft_for_review", email="one@x.com")
    c2 = await _setup(db, a, mode="draft_for_review", email="two@x.com")
    now = datetime.now(UTC)
    for cid in (c1, c2):
        await planner.create_plan(db, a, contact_id=cid, kind="outbound_manual", goal=None,
                                  now=now)
    await dispatch.dispatch_due_tasks(db, limit=10, now=now)
    drafts = await repo.list_messages(db, a, status="draft", limit=10, offset=0)
    assert len(drafts) == 2
    by_contact = {m.contact_id: m for m in drafts}

    await service.discard(db, a, by_contact[c1].message_id)
    await db.execute("UPDATE email_messages SET created_at = now() - interval '8 days' "
                     "WHERE message_id = $1", by_contact[c2].message_id)
    assert await service.expire_drafts(db, ttl_days=7) == 1
    rows = await db.fetch("SELECT status, last_error FROM outreach_tasks WHERE step_index = 0 "
                          "ORDER BY last_error")
    assert [(r["status"], r["last_error"]) for r in rows] == [
        ("cancelled", "DISCARDED"), ("cancelled", "DRAFT_EXPIRED")]


async def test_transient_failure_retries_with_the_same_message_id(env: dict[str, Any]) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a)
    mailer.fail_next = 1
    now = datetime.now(UTC)
    await _plan_and_dispatch(db, a, contact_id, now)
    task = await _task(db)
    assert task["status"] == "scheduled" and task["attempts"] == 1
    message = await repo.get_message_for_task(db, a, task["task_id"])
    assert message is not None and message.status == "sending"

    await dispatch.dispatch_due_tasks(db, limit=10, now=now + timedelta(minutes=5))
    assert (await _task(db))["status"] == "completed"
    assert [e.message_id for e in mailer.sent] == [message.rfc_message_id]
    assert await db.fetchval("SELECT count(*) FROM email_messages") == 1


async def test_second_email_threads_under_the_first(env: dict[str, Any]) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id = await _setup(db, a)
    now = datetime.now(UTC)
    await _plan_and_dispatch(db, a, contact_id, now)
    await dispatch.dispatch_due_tasks(db, limit=10, now=now + timedelta(days=4, minutes=1))
    first, second = mailer.sent
    assert second.in_reply_to == first.message_id
    assert second.references == [first.message_id]
    assert second.subject == f"Re: {first.subject}"
    assert await db.fetchval("SELECT count(*) FROM email_threads") == 1


async def test_meeting_booked_hook_stops_the_plan(env: dict[str, Any]) -> None:
    db, a = env["db"], _claims(env["a"])
    contact_id = await _setup(db, a, mode="draft_for_review")
    await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual", goal=None,
                              now=datetime.now(UTC))
    await hooks.meeting_booked(db, a, email="nobody@x.com")  # unknown -> nothing
    await hooks.meeting_booked(db, a, email="JANE@example.com")
    assert await db.fetchval("SELECT count(*) FROM agent_events "
                             "WHERE kind = 'meeting_booked'") == 1
    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT stop_reason FROM outreach_plans") == "meeting_booked"


# -- HTTP: unsubscribe + routes --------------------------------------------------------------


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
async def client(env: dict[str, Any]) -> AsyncIterator[AsyncClient]:
    from api.app import create_app

    app = create_app()
    app.state.db, app.state.redis, app.state.cache = env["db"], _Redis(), InMemoryCache()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _cookie(tenant: str | None, role: Role = Role.CLIENT_ADMIN, subject: str = "admin-a"
            ) -> dict[str, str]:
    token, _ = create_access_token(AuthClaims(subject=subject, role=role, tenant_id=tenant),
                                   secret=_JWT, ttl_seconds=300)
    return {"access_token": token}


async def test_one_click_unsubscribe_suppresses_and_stops_plan(
    env: dict[str, Any], client: AsyncClient,
) -> None:
    db, a = env["db"], _claims(env["a"])
    contact_id = await _setup(db, a, mode="draft_for_review")
    await planner.create_plan(db, a, contact_id=contact_id, kind="outbound_manual", goal=None,
                              now=datetime.now(UTC))
    token = sign_unsubscribe(env["a"], contact_id, "jane@example.com")
    url = f"/public/email/unsubscribe/{token}"

    page = await client.get(url)
    assert page.status_code == 200 and "Unsubscribe" in page.text
    assert await db.fetchval("SELECT count(*) FROM suppression_list") == 0  # GET never acts

    for _ in range(2):  # idempotent
        r = await client.post(url, content="List-Unsubscribe=One-Click",
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200
    rows = await db.fetch("SELECT tenant_id, channel, value_normalised FROM suppression_list")
    assert [tuple(r) for r in rows] == [(env["a"], "email", "jane@example.com")]
    assert await db.fetchval("SELECT count(*) FROM agent_events "
                             "WHERE kind = 'opt_out_received'") == 1
    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT status FROM outreach_plans") == "stopped"

    assert (await client.post("/public/email/unsubscribe/forged.token")).status_code == 404


async def test_email_config_and_settings_rbac(env: dict[str, Any], client: AsyncClient) -> None:
    a = env["a"]
    admin, agent = _cookie(a), _cookie(a, Role.CLIENT_AGENT, "agent-a")
    body = {"provider": "sendgrid", "from_address": "Rebecca@Mail.Acme.com", "from_name": "Acme",
            "api_key": "SG.secret-value"}
    # Enabling email before a sender exists is refused.
    r = await client.put("/admin/outreach/settings", cookies=admin, json={
        "enabled_channels": ["email"], "physical_address": _ADDRESS})
    assert r.status_code == 422 and r.json()["error_code"] == "EMAIL_NOT_READY"

    assert (await client.put("/admin/outreach/email-config", json=body,
                             cookies=agent)).status_code == 403
    r = await client.put("/admin/outreach/email-config", json=body, cookies=admin)
    assert r.status_code == 200
    got = (await client.get("/admin/outreach/email-config", cookies=agent)).json()
    assert got["has_api_key"] is True and got["from_address"] == "rebecca@mail.acme.com"
    assert "SG.secret-value" not in str(got)
    stored = await env["db"].fetchval("SELECT api_key_ciphertext FROM tenant_email_agent_configs")
    assert "SG.secret-value" not in stored

    r = await client.put("/admin/outreach/settings", cookies=admin,
                         json={"enabled_channels": ["email"]})
    assert r.status_code == 422  # still no physical address
    r = await client.put("/admin/outreach/settings", cookies=admin, json={
        "enabled_channels": ["email"], "physical_address": _ADDRESS})
    assert r.status_code == 200

    # Tenant B sees nothing of A's config.
    assert (await client.get("/admin/outreach/email-config",
                             cookies=_cookie(env["b"], subject="admin-b"))).json() == {
        "configured": False}


async def test_review_queue_rbac_and_isolation(env: dict[str, Any], client: AsyncClient) -> None:
    db, a = env["db"], _claims(env["a"])
    own = await _setup(db, a, mode="draft_for_review", email="own@x.com", owner="agent-a")
    other = await _setup(db, a, mode="draft_for_review", email="other@x.com")
    now = datetime.now(UTC)
    for cid in (own, other):
        await planner.create_plan(db, a, contact_id=cid, kind="outbound_manual", goal=None,
                                  now=now)
    await dispatch.dispatch_due_tasks(db, limit=10, now=now)
    drafts = {m.contact_id: m.message_id
              for m in await repo.list_messages(db, a, status="draft", limit=10, offset=0)}

    admin = _cookie(env["a"])
    agent = _cookie(env["a"], Role.CLIENT_AGENT, "agent-a")
    visitor = _cookie(env["a"], Role.VISITOR, "v-1")
    admin_b = _cookie(env["b"], subject="admin-b")

    assert len((await client.get("/admin/outreach/emails?status=draft", cookies=admin)).json()) == 2
    agent_list = (await client.get("/admin/outreach/emails?status=draft", cookies=agent)).json()
    assert [m["contact_id"] for m in agent_list] == [own]
    assert (await client.get(f"/admin/outreach/emails/{drafts[other]}",
                             cookies=agent)).status_code == 404
    assert (await client.get(f"/admin/outreach/emails/{drafts[own]}",
                             cookies=admin_b)).status_code == 404
    assert (await client.get("/admin/outreach/emails", cookies=visitor)).status_code == 403

    r = await client.put(f"/admin/outreach/emails/{drafts[own]}", cookies=agent,
                         json={"subject": "Edited", "body_text": "Edited body"})
    assert r.status_code == 200 and r.json()["subject"] == "Edited"
    r = await client.post(f"/admin/outreach/emails/{drafts[own]}:approve", cookies=agent)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert env["mailer"].sent[0].subject == "Edited"
    assert (await client.post(f"/admin/outreach/emails/{drafts[other]}:discard",
                              cookies=admin_b)).status_code == 404
    r = await client.post(f"/admin/outreach/emails/{drafts[other]}:discard", cookies=admin)
    assert r.status_code == 200
    platform = _cookie(None, Role.PLATFORM_ADMIN, "root")
    r = await client.get(f"/admin/tenants/{env['a']}/outreach/emails", cookies=platform)
    assert r.status_code == 200 and len(r.json()) == 2
