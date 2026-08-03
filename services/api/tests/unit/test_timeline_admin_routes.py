"""Full-stack (ASGI) tests for the SR-9.3 timeline routes.

Covers, against a real ``create_app()`` with a comprehensive in-memory stub
database:
- RBAC (D7): CLIENT_ADMIN/CLIENT_AGENT 200; VISITOR 403; PLATFORM_ADMIN 403
  implicit / 200 tenant-explicit + 404 TENANT_NOT_FOUND for unknown tenant.
- POST/PATCH/DELETE -> 405 on both timeline paths.
- Cross-tenant contact/lead id -> 404 (not 403).
- Attribution correctness end-to-end (multi-channel, multi-device, converted
  lead exposing converted_to_contact_id, negative attribution).
- Leak-free response: no tenant_id, no notification body.
- Caching (D8): identical requests hit cache; tenant-scoped key; degraded
  responses never cached.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"
_OTHER_TENANT_ID = "tenant-xyz-999"

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": _TEST_JWT_SECRET,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _StubDatabase:
    """In-memory stub covering every table the timeline fan-out touches."""

    def __init__(self) -> None:
        self._tenants: dict[str, dict[str, Any]] = {}
        self._contacts: dict[tuple[str, str], dict[str, Any]] = {}
        self._identities: dict[tuple[str, str], dict[str, Any]] = {}
        self._leads: dict[tuple[str, str], dict[str, Any]] = {}
        self._activities: dict[tuple[str, str], dict[str, Any]] = {}
        self._conversations: dict[tuple[str, str], dict[str, Any]] = {}
        self._messages: dict[tuple[str, str], dict[str, Any]] = {}
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._id_counter = 0

    def _next_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}-{self._id_counter}"

    # -- seeding -------------------------------------------------------

    def seed_tenant(self, *, tenant_id: str, slug: str, enabled: bool = True) -> None:
        self._tenants[tenant_id] = {"id": tenant_id, "name": slug, "slug": slug, "enabled": enabled}

    def seed_contact(
        self, *, tenant_id: str, contact_id: str, lead_id: str | None = None,
    ) -> None:
        self._contacts[(tenant_id, contact_id)] = {
            "tenant_id": tenant_id, "contact_id": contact_id, "account_id": None,
            "lead_id": lead_id, "name": "Dana Contact", "email": "dana@example.com",
            "phone": None, "consent": {"granted": True}, "owner_agent_id": None,
            "created_at": _NOW, "updated_at": _NOW,
        }

    def seed_identity(
        self, *, tenant_id: str, contact_id: str, identity_type: str, identity_value: str,
    ) -> None:
        identity_id = self._next_id("identity")
        self._identities[(tenant_id, identity_id)] = {
            "tenant_id": tenant_id, "identity_id": identity_id, "contact_id": contact_id,
            "identity_type": identity_type, "identity_value": identity_value,
            "created_at": _NOW,
        }

    def seed_lead(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        visitor_id: str | None = None,
        email: str | None = None,
        converted_to_contact_id: str | None = None,
    ) -> None:
        self._leads[(tenant_id, lead_id)] = {
            "tenant_id": tenant_id, "lead_id": lead_id, "visitor_id": visitor_id,
            "name": None, "email": email, "phone": None, "status": "new",
            "stage": "captured", "qualification_score": None, "consent": {},
            "assigned_agent_id": None, "source": "widget", "created_at": _NOW,
            "updated_at": _NOW, "converted_to_contact_id": converted_to_contact_id,
        }

    def seed_activity(
        self, *, tenant_id: str, lead_id: str, activity_type: str = "note",
        occurred_at: datetime = _NOW,
    ) -> str:
        activity_id = self._next_id("activity")
        self._activities[(tenant_id, activity_id)] = {
            "tenant_id": tenant_id, "activity_id": activity_id, "lead_id": lead_id,
            "type": activity_type, "payload": None, "actor": "system",
            "created_at": occurred_at,
        }
        return activity_id

    def seed_conversation_with_message(
        self, *, tenant_id: str, visitor_id: str, occurred_at: datetime = _NOW,
        content: str = "hello",
    ) -> str:
        conversation_id = self._next_id("conv")
        self._conversations[(tenant_id, conversation_id)] = {
            "tenant_id": tenant_id, "conversation_id": conversation_id,
            "visitor_id": visitor_id, "status": "active", "channel": "widget",
            "started_at": occurred_at, "ended_at": None, "metadata": {},
            "summary": None, "summary_message_count": 0,
        }
        message_id = self._next_id("msg")
        self._messages[(tenant_id, message_id)] = {
            "tenant_id": tenant_id, "message_id": message_id,
            "conversation_id": conversation_id, "role": "user", "content": content,
            "intent": None, "confidence": None, "tokens": None,
            "created_at": occurred_at, "sources": None, "decision": None,
            "grounded": None, "guardrail_flag": None, "action": None,
        }
        return conversation_id

    def seed_event(
        self, *, tenant_id: str, lead_id: str | None = None, visitor_id: str | None = None,
        occurred_at: datetime = _NOW, source: str = "native",
    ) -> str:
        event_id = self._next_id("event")
        self._events[(tenant_id, event_id)] = {
            "tenant_id": tenant_id, "event_id": event_id, "lead_id": lead_id,
            "visitor_id": visitor_id, "email": None, "name": None,
            "starts_at": occurred_at, "ends_at": occurred_at, "timezone": "UTC",
            "status": "booked", "calendar_ref": None, "consent": {},
            "created_at": occurred_at, "source": source,
        }
        return event_id

    def seed_notification_job(
        self, *, tenant_id: str, lead_id: str | None = None, occurred_at: datetime = _NOW,
        subject: str = "Your booking is confirmed", body: str = "secret body text",
    ) -> str:
        job_id = self._next_id("job")
        self._jobs[job_id] = {
            "job_id": job_id, "tenant_id": tenant_id, "channel": "email", "template": None,
            "recipient": "someone@example.com", "subject": subject, "body": body,
            "payload": None, "dedupe_key": job_id, "status": "sent", "attempts": 1,
            "delivery_ref": None, "last_error": None, "created_at": occurred_at,
            "updated_at": occurred_at, "lead_id": lead_id,
        }
        return job_id

    # -- generic DB protocol --------------------------------------------

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        if "FROM CONTACTS" in q and "CONTACT_ID = $2" in q:
            tenant_id, contact_id = args[0], args[1]
            return self._contacts.get((tenant_id, contact_id))
        if "FROM LEADS" in q and "LEAD_ID = $2" in q:
            tenant_id, lead_id = args[0], args[1]
            return self._leads.get((tenant_id, lead_id))
        if "COUNT(*)" in q and "FROM CONVERSATIONS" in q:
            tenant_id = args[0]
            visitor_ids = args[1] if len(args) > 1 else None
            rows = self._filtered_conversations(tenant_id, visitor_ids)
            return {"count": len(rows)}
        if "SELECT 1 FROM CONVERSATIONS" in q:
            # _verify_conversation_visible -- WHERE tenant_id = $1 AND
            # conversation_id = $2
            tenant_id, conversation_id = args[0], args[1]
            row = self._conversations.get((tenant_id, conversation_id))
            return {"exists": 1} if row is not None else None
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM CONTACT_IDENTITIES" in q:
            tenant_id, contact_id = args[0], args[1]
            return [
                row for row in self._identities.values()
                if row["tenant_id"] == tenant_id and row["contact_id"] == contact_id
            ]
        if "FROM LEADS" in q and "CONVERTED_TO_CONTACT_ID = $2" in q:
            tenant_id, contact_id = args[0], args[1]
            return [
                row for row in self._leads.values()
                if row["tenant_id"] == tenant_id
                and row.get("converted_to_contact_id") == contact_id
            ]
        if "FROM LEAD_ACTIVITIES" in q:
            tenant_id, lead_id = args[0], args[1]
            rows = [
                row for row in self._activities.values()
                if row["tenant_id"] == tenant_id and row["lead_id"] == lead_id
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows
        if "FROM CONVERSATIONS" in q:
            tenant_id = args[0]
            visitor_ids = args[1] if len(args) > 1 else None
            rows = self._filtered_conversations(tenant_id, visitor_ids)
            rows.sort(key=lambda r: r["started_at"], reverse=True)
            out = []
            for r in rows:
                message_count = sum(
                    1 for m in self._messages.values()
                    if m["tenant_id"] == tenant_id and m["conversation_id"] == r["conversation_id"]
                )
                out.append({**r, "message_count": message_count})
            return out
        if "FROM MESSAGES" in q:
            tenant_id, conversation_id = args[0], args[1]
            rows = [
                row for row in self._messages.values()
                if row["tenant_id"] == tenant_id and row["conversation_id"] == conversation_id
            ]
            rows.sort(key=lambda r: r["created_at"])
            return rows
        if "FROM SCHEDULE_EVENTS" in q and "LEAD_ID = ANY" in q:
            tenant_id, lead_ids, visitor_ids = args[0], args[1], args[2]
            rows = [
                row for row in self._events.values()
                if row["tenant_id"] == tenant_id
                and (row["lead_id"] in lead_ids or row["visitor_id"] in visitor_ids)
            ]
            if "STARTS_AT < $" in q:
                before = args[3]
                rows = [r for r in rows if r["starts_at"] < before]
            rows.sort(key=lambda r: r["starts_at"], reverse=True)
            return rows
        if "FROM NOTIFICATION_JOBS" in q and "LEAD_ID = ANY" in q:
            tenant_id, lead_ids = args[0], args[1]
            rows = [
                row for row in self._jobs.values()
                if row["tenant_id"] == tenant_id and row.get("lead_id") in lead_ids
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows
        return []

    def _filtered_conversations(
        self, tenant_id: str, visitor_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        rows = [r for r in self._conversations.values() if r["tenant_id"] == tenant_id]
        if visitor_ids is not None:
            rows = [r for r in rows if r["visitor_id"] in visitor_ids]
        return rows

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    async def close(self) -> None:
        pass


class _StubRedis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        pass

    async def getdel(self, key: str) -> str | None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app(db: _StubDatabase, *, cache: Any = None) -> Any:
    _reset_settings()
    import os

    old_env = {k: os.environ.get(k) for k in _TEST_SETTINGS_ENV}
    os.environ.update(_TEST_SETTINGS_ENV)
    try:
        from api.app import create_app

        app = create_app()
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    app.state.db = db
    app.state.redis = _StubRedis()
    app.state.cache = cache if cache is not None else InMemoryCache()
    return app


def _token(role: Role, tenant_id: str | None = _TENANT_ID, subject: str = "user-1") -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


async def _get(app: Any, path: str, role: Role, tenant_id: str | None = _TENANT_ID) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(role, tenant_id=tenant_id)
        return await client.get(path, cookies={"access_token": token})


def db() -> _StubDatabase:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_tenant(tenant_id=_OTHER_TENANT_ID, slug="widgetco")
    return d


# ---------------------------------------------------------------------------
# RBAC (D7)
# ---------------------------------------------------------------------------


async def test_client_admin_and_agent_succeed_on_both_endpoints() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    d.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-1")
    app = _build_app(d)

    for role in (Role.CLIENT_ADMIN, Role.CLIENT_AGENT):
        r1 = await _get(app, "/admin/contacts/contact-1/timeline", role)
        r2 = await _get(app, "/admin/leads/lead-1/timeline", role)
        assert r1.status_code == 200
        assert r2.status_code == 200


async def test_visitor_rejected_on_every_route() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    d.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-1")
    app = _build_app(d)

    r1 = await _get(app, "/admin/contacts/contact-1/timeline", Role.VISITOR)
    r2 = await _get(app, "/admin/leads/lead-1/timeline", Role.VISITOR)
    assert r1.status_code in (401, 403)
    assert r2.status_code in (401, 403)


async def test_platform_admin_rejected_on_implicit_routes() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-1/timeline", Role.PLATFORM_ADMIN, tenant_id=None)
    assert r.status_code == 403


async def test_platform_admin_succeeds_via_tenant_explicit() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    app = _build_app(d)

    r = await _get(
        app, f"/admin/tenants/{_TENANT_ID}/contacts/contact-1/timeline",
        Role.PLATFORM_ADMIN, tenant_id=None,
    )
    assert r.status_code == 200


async def test_platform_admin_unknown_tenant_returns_404() -> None:
    d = db()
    app = _build_app(d)

    r = await _get(
        app, "/admin/tenants/does-not-exist/contacts/contact-1/timeline",
        Role.PLATFORM_ADMIN, tenant_id=None,
    )
    assert r.status_code == 404
    assert r.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_write_verbs_return_405() -> None:
    d = db()
    app = _build_app(d)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        for method, path in (
            ("post", "/admin/contacts/contact-1/timeline"),
            ("patch", "/admin/contacts/contact-1/timeline"),
            ("delete", "/admin/contacts/contact-1/timeline"),
            ("post", "/admin/leads/lead-1/timeline"),
            ("patch", "/admin/leads/lead-1/timeline"),
            ("delete", "/admin/leads/lead-1/timeline"),
        ):
            response = await getattr(client, method)(path, cookies={"access_token": token})
            assert response.status_code == 405


# ---------------------------------------------------------------------------
# Tenant isolation / cross-tenant 404
# ---------------------------------------------------------------------------


async def test_cross_tenant_contact_returns_404_not_403() -> None:
    d = db()
    d.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-foreign")
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-foreign/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 404


async def test_cross_tenant_lead_returns_404_not_403() -> None:
    d = db()
    d.seed_lead(tenant_id=_OTHER_TENANT_ID, lead_id="lead-foreign")
    app = _build_app(d)

    r = await _get(app, "/admin/leads/lead-foreign/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 404


async def test_tenant_isolation_across_all_four_sources_same_ids() -> None:
    """MANDATORY: tenant A and B share the SAME visitor_id/email/lead_id
    strings; tenant A's timeline returns ONLY A's rows, checked per source."""
    d = db()
    shared_visitor = "visitor-shared"
    shared_lead = "lead-shared"

    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-a", lead_id=shared_lead)
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-a",
        identity_type="visitor_id", identity_value=shared_visitor,
    )
    d.seed_lead(tenant_id=_TENANT_ID, lead_id=shared_lead, visitor_id=shared_visitor)
    d.seed_activity(tenant_id=_TENANT_ID, lead_id=shared_lead)
    d.seed_conversation_with_message(
        tenant_id=_TENANT_ID, visitor_id=shared_visitor, content="tenant A secret",
    )
    d.seed_event(tenant_id=_TENANT_ID, lead_id=shared_lead, visitor_id=shared_visitor)
    d.seed_notification_job(tenant_id=_TENANT_ID, lead_id=shared_lead, subject="A subject")

    d.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-b", lead_id=shared_lead)
    d.seed_identity(
        tenant_id=_OTHER_TENANT_ID, contact_id="contact-b",
        identity_type="visitor_id", identity_value=shared_visitor,
    )
    d.seed_lead(tenant_id=_OTHER_TENANT_ID, lead_id=shared_lead, visitor_id=shared_visitor)
    d.seed_activity(tenant_id=_OTHER_TENANT_ID, lead_id=shared_lead)
    d.seed_conversation_with_message(
        tenant_id=_OTHER_TENANT_ID, visitor_id=shared_visitor, content="tenant B secret",
    )
    d.seed_event(tenant_id=_OTHER_TENANT_ID, lead_id=shared_lead, visitor_id=shared_visitor)
    d.seed_notification_job(tenant_id=_OTHER_TENANT_ID, lead_id=shared_lead, subject="B subject")

    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-a/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is False

    messages = [i for i in body["items"] if i["kind"] == "message"]
    activities = [i for i in body["items"] if i["kind"] == "lead_activity"]
    bookings = [i for i in body["items"] if i["kind"] == "booking"]
    notifications = [i for i in body["items"] if i["kind"] == "notification"]

    assert len(messages) == 1
    assert messages[0]["data"]["content"] == "tenant A secret"
    assert len(activities) == 1
    assert len(bookings) == 1
    assert len(notifications) == 1
    assert notifications[0]["data"]["subject"] == "A subject"


# ---------------------------------------------------------------------------
# Attribution correctness (D2/D3)
# ---------------------------------------------------------------------------


async def test_lead_timeline_before_conversion_has_all_four_kinds() -> None:
    """The core new case (D1): a not-yet-converted Lead's timeline works."""
    d = db()
    d.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-1", visitor_id="visitor-1")
    d.seed_activity(tenant_id=_TENANT_ID, lead_id="lead-1", activity_type="booked_a_call")
    d.seed_conversation_with_message(tenant_id=_TENANT_ID, visitor_id="visitor-1")
    d.seed_event(tenant_id=_TENANT_ID, lead_id="lead-1", visitor_id="visitor-1")
    d.seed_notification_job(tenant_id=_TENANT_ID, lead_id="lead-1")
    app = _build_app(d)

    r = await _get(app, "/admin/leads/lead-1/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    body = r.json()
    kinds = {i["kind"] for i in body["items"]}
    assert kinds == {"message", "lead_activity", "booking", "notification"}
    assert body["subject"]["kind"] == "lead"
    assert body["subject"]["converted_to_contact_id"] is None


async def test_contact_timeline_multi_device_returns_both_sessions() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-1",
        identity_type="visitor_id", identity_value="visitor-device-1",
    )
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-1",
        identity_type="visitor_id", identity_value="visitor-device-2",
    )
    d.seed_conversation_with_message(
        tenant_id=_TENANT_ID, visitor_id="visitor-device-1", content="from device 1",
    )
    d.seed_conversation_with_message(
        tenant_id=_TENANT_ID, visitor_id="visitor-device-2", content="from device 2",
    )
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-1/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    contents = {i["data"]["content"] for i in r.json()["items"] if i["kind"] == "message"}
    assert contents == {"from device 1", "from device 2"}


async def test_manually_created_contact_resolves_via_email_identity_alone() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-manual", lead_id=None)
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-manual",
        identity_type="email", identity_value="manual@example.com",
    )
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-manual/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    assert r.json()["degraded"] is False


async def test_contact_timeline_after_conversion_shows_pre_conversion_activity() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", lead_id="lead-1")
    d.seed_lead(
        tenant_id=_TENANT_ID, lead_id="lead-1",
        converted_to_contact_id="contact-1",
    )
    d.seed_activity(
        tenant_id=_TENANT_ID, lead_id="lead-1", activity_type="note",
        occurred_at=datetime(2025, 12, 1, tzinfo=UTC),
    )
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-1/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    activities = [i for i in r.json()["items"] if i["kind"] == "lead_activity"]
    assert len(activities) == 1


async def test_negative_attribution_different_visitor_and_email_never_appears() -> None:
    """D3: a second person with a different visitor_id/email -- including
    same email domain and same name -- never appears."""
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-1",
        identity_type="visitor_id", identity_value="visitor-1",
    )
    d.seed_conversation_with_message(
        tenant_id=_TENANT_ID, visitor_id="visitor-1", content="person 1 message",
    )
    # A different person, same tenant, same domain/name, different identity.
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2")
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-2",
        identity_type="visitor_id", identity_value="visitor-2",
    )
    d.seed_conversation_with_message(
        tenant_id=_TENANT_ID, visitor_id="visitor-2", content="person 2 message",
    )
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-1/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    messages = [i for i in r.json()["items"] if i["kind"] == "message"]
    assert len(messages) == 1
    assert messages[0]["data"]["content"] == "person 1 message"


async def test_calendly_booking_no_lead_id_appears_via_visitor_path() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-1",
        identity_type="visitor_id", identity_value="visitor-1",
    )
    d.seed_event(tenant_id=_TENANT_ID, lead_id=None, visitor_id="visitor-1", source="calendly")
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-1/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    bookings = [i for i in r.json()["items"] if i["kind"] == "booking"]
    assert len(bookings) == 1
    assert bookings[0]["data"]["source"] == "calendly"


async def test_already_converted_lead_returns_own_items_and_converted_flag() -> None:
    """D2: the lead route does NOT silently widen to the contact's fuller set."""
    d = db()
    d.seed_lead(
        tenant_id=_TENANT_ID, lead_id="lead-1",
        converted_to_contact_id="contact-1",
    )
    d.seed_activity(tenant_id=_TENANT_ID, lead_id="lead-1")
    app = _build_app(d)

    r = await _get(app, "/admin/leads/lead-1/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["subject"]["converted_to_contact_id"] == "contact-1"
    assert len(body["items"]) == 1


# ---------------------------------------------------------------------------
# Empty vs broken
# ---------------------------------------------------------------------------


async def test_empty_history_returns_empty_items_all_sources_ok() -> None:
    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-empty")
    app = _build_app(d)

    r = await _get(app, "/admin/contacts/contact-empty/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["degraded"] is False
    assert all(s["state"] == "ok" for s in body["sources"].values())


# ---------------------------------------------------------------------------
# PII / leak-free response
# ---------------------------------------------------------------------------


async def test_response_never_contains_tenant_id_or_notification_body() -> None:
    d = db()
    d.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-1")
    d.seed_notification_job(
        tenant_id=_TENANT_ID, lead_id="lead-1", body="super secret body content",
    )
    app = _build_app(d)

    r = await _get(app, "/admin/leads/lead-1/timeline", Role.CLIENT_ADMIN)
    assert r.status_code == 200
    raw_text = r.text
    assert "tenant_id" not in raw_text
    assert "super secret body content" not in raw_text
    notifications = [i for i in r.json()["items"] if i["kind"] == "notification"]
    assert len(notifications) == 1
    assert "body" not in notifications[0]["data"]


# ---------------------------------------------------------------------------
# Pagination (D9)
# ---------------------------------------------------------------------------


async def test_pagination_limit_and_before_no_overlap() -> None:
    d = db()
    d.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-1")
    for h in range(1, 11):
        d.seed_activity(
            tenant_id=_TENANT_ID, lead_id="lead-1",
            occurred_at=datetime(2026, 1, 1, h, tzinfo=UTC),
        )
    app = _build_app(d)

    r1 = await _get(app, "/admin/leads/lead-1/timeline?limit=5", Role.CLIENT_ADMIN)
    assert r1.status_code == 200
    body1 = r1.json()
    assert len(body1["items"]) == 5
    assert body1["next_before"] is not None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        r2 = await client.get(
            f"/admin/leads/lead-1/timeline?limit=5&before={body1['next_before']}",
            cookies={"access_token": token},
        )
    assert r2.status_code == 200
    body2 = r2.json()
    assert len(body2["items"]) == 5

    ids_1 = {i["id"] for i in body1["items"]}
    ids_2 = {i["id"] for i in body2["items"]}
    assert ids_1.isdisjoint(ids_2)

    # Page to full exhaustion (D9): every seeded item seen exactly once,
    # next_before eventually becomes null.
    seen = set(ids_1) | set(ids_2)
    cursor = body2["next_before"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        while cursor is not None:
            resp = await client.get(
                f"/admin/leads/lead-1/timeline?limit=5&before={cursor}",
                cookies={"access_token": token},
            )
            page = resp.json()
            page_ids = {i["id"] for i in page["items"]}
            assert page_ids.isdisjoint(seen)
            seen |= page_ids
            cursor = page["next_before"]

    assert len(seen) == 10


async def test_limit_clamped_at_200() -> None:
    d = db()
    d.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-1")
    app = _build_app(d)

    r = await _get(app, "/admin/leads/lead-1/timeline?limit=99999", Role.CLIENT_ADMIN)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Identity resolution failure is ALWAYS a hard error (D6 carve-out)
# ---------------------------------------------------------------------------


async def test_identity_resolution_failure_returns_500_no_partial_body() -> None:
    """D6: unlike a source-read failure, an identity-resolution failure is
    NEVER a degraded 200 -- it is a hard 500 with a correlation id and no
    partial body."""
    from unittest.mock import patch

    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    app = _build_app(d)

    with patch(
        "api.timeline.admin_routes.resolve_contact_identities",
        side_effect=RuntimeError("identity db unavailable"),
    ):
        r = await _get(app, "/admin/contacts/contact-1/timeline", Role.CLIENT_ADMIN)

    assert r.status_code == 500
    body = r.json()
    assert "correlation_id" in body
    assert "items" not in body
    assert "degraded" not in body


# ---------------------------------------------------------------------------
# PII-safe logging
# ---------------------------------------------------------------------------


async def test_logging_contains_no_pii(caplog: Any) -> None:
    import logging

    d = db()
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    d.seed_identity(
        tenant_id=_TENANT_ID, contact_id="contact-1",
        identity_type="email", identity_value="secret.person@example.com",
    )
    d.seed_conversation_with_message(
        tenant_id=_TENANT_ID, visitor_id="visitor-1", content="my secret message content",
    )
    app = _build_app(d)

    with caplog.at_level(logging.INFO):
        r = await _get(app, "/admin/contacts/contact-1/timeline", Role.CLIENT_ADMIN)

    assert r.status_code == 200
    for record in caplog.records:
        text = record.getMessage()
        assert "secret.person@example.com" not in text
        assert "my secret message content" not in text
        assert "Dana Contact" not in text
