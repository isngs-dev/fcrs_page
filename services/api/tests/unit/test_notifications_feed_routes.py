"""Unit tests for the SR-21 notifications feed routes -- GET /admin/notifications,
PATCH /admin/notifications/{event_id}/read, PATCH /admin/notifications/read-all,
and their /admin/tenants/{tenant_id}/notifications tenant-scoped twins (M3/M4).

Covers (spec "Tests" section):
- RBAC: CLIENT_ADMIN and CLIENT_AGENT both list and mark-read; VISITOR
  rejected on every feed endpoint; PLATFORM_ADMIN rejected on implicit
  routes, succeeds on tenant-explicit twins, 404 TENANT_NOT_FOUND for an
  unknown tenant.
- The pills filter real data: ?category=leads / ?category=system.
- No `mentions` category anywhere -- ?category=mentions is rejected (D4).
- Tenant isolation: cross-tenant PATCH on another tenant's event_id -> 404,
  no existence leak.
- Per-user read state (D6): user X marking read does NOT affect user Y in
  the same tenant -- the single highest-value test in this sprint.
- Bounds (D7): limit is clamped server-side.
- No response body contains tenant_id.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_A = "tenant-a-111"
_TENANT_B = "tenant-b-222"

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


class _StubDatabase:
    """In-memory stub covering notification_events + notification_event_reads,
    plus a minimal tenants table read for resolve_tenant_scope."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self._reads: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._tenants: dict[str, dict[str, Any]] = {
            _TENANT_A: {"tenant_id": _TENANT_A, "enabled": True},
            _TENANT_B: {"tenant_id": _TENANT_B, "enabled": True},
        }

    def seed_event(
        self, *, tenant_id: str, event_id: str, kind: str, category: str,
        target_type: str | None = "lead", target_id: str | None = "lead-1",
        payload: dict[str, Any] | None = None, created_at: datetime | None = None,
    ) -> None:
        self._events[(tenant_id, event_id)] = {
            "tenant_id": tenant_id,
            "event_id": event_id,
            "kind": kind,
            "category": category,
            "target_type": target_type,
            "target_id": target_id,
            "payload": payload or {"lead_id": target_id},
            "actor_id": None,
            "created_at": created_at or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        }

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO NOTIFICATION_EVENT_READS"):
            tenant_id, event_id, user_id, read_at = args
            if (tenant_id, event_id) not in self._events:
                return "INSERT 0 0"
            key = (tenant_id, event_id, user_id)
            if key in self._reads:
                return "INSERT 0 0"
            self._reads[key] = {"read_at": read_at}
            return "INSERT 0 1"
        if q.startswith("DELETE FROM NOTIFICATION_EVENT_READS"):
            tenant_id, event_id, user_id = args
            existed = (tenant_id, event_id, user_id) in self._reads
            self._reads.pop((tenant_id, event_id, user_id), None)
            return "DELETE 1" if existed else "DELETE 0"
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM NOTIFICATION_EVENTS" in q and "EVENT_ID = $" in q:
            tenant_id, event_id = args[0], args[1]
            return self._events.get((tenant_id, event_id))
        if "FROM TENANTS" in q:
            tenant_id = args[0]
            return self._tenants.get(tenant_id)
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = query.strip().upper()
        if "COUNT(*)" not in q or "FROM NOTIFICATION_EVENTS" not in q:
            return 0
        is_bare_unread_count = "NOT EXISTS" in q and "CATEGORY = $" not in q and len(args) == 2
        if is_bare_unread_count:
            tenant_id, user_id = args[0], args[1]
            return sum(
                1 for (t_id, e_id) in self._events
                if t_id == tenant_id and (t_id, e_id, user_id) not in self._reads
            )
        idx = 1
        tenant_id = args[0]
        rows = [r for r in self._events.values() if r["tenant_id"] == tenant_id]
        if "CATEGORY = $" in q:
            category = args[idx]
            idx += 1
            rows = [r for r in rows if r["category"] == category]
        if "NOT EXISTS" in q:
            user_id = args[idx]
            idx += 1
            rows = [
                r for r in rows if (r["tenant_id"], r["event_id"], user_id) not in self._reads
            ]
        return len(rows)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM NOTIFICATION_EVENTS" in q:
            tenant_id = args[0]
            rows = [dict(r) for r in self._events.values() if r["tenant_id"] == tenant_id]
            idx = 1
            if "CATEGORY = $" in q:
                category = args[idx]
                idx += 1
                rows = [r for r in rows if r["category"] == category]
            user_id = args[idx]
            idx += 1
            rows.sort(key=lambda r: (r["created_at"], r["event_id"]), reverse=True)
            for r in rows:
                r["read"] = (tenant_id, r["event_id"], user_id) in self._reads
            if q.count("EXISTS") >= 2:
                rows = [r for r in rows if not r["read"]]
            limit = args[idx] if idx < len(args) else None
            offset = args[idx + 1] if idx + 1 < len(args) else 0
            if limit is not None:
                rows = rows[offset: offset + limit]
            return rows
        return []

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


def _build_app(db: _StubDatabase) -> Any:
    _reset_settings()
    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()
    app.state.db = db
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    return app


def _token(role: Role, tenant_id: str | None, subject: str = "user-1") -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


# ---------------------------------------------------------------------------
# GET /admin/notifications -- RBAC
# ---------------------------------------------------------------------------


async def test_client_admin_can_list() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_A)
        resp = await client.get("/admin/notifications", cookies={"access_token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1


async def test_client_agent_can_list() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, _TENANT_A)
        resp = await client.get("/admin/notifications", cookies={"access_token": token})
    assert resp.status_code == 200


async def test_visitor_forbidden_on_list() -> None:
    db = _StubDatabase()
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR, _TENANT_A)
        resp = await client.get("/admin/notifications", cookies={"access_token": token})
    assert resp.status_code == 403


async def test_visitor_forbidden_on_mark_read() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR, _TENANT_A)
        resp = await client.patch(
            "/admin/notifications/e1/read", json={"read": True}, cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_visitor_forbidden_on_mark_all_read() -> None:
    db = _StubDatabase()
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR, _TENANT_A)
        resp = await client.patch(
            "/admin/notifications/read-all", json={}, cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_no_auth_returns_401() -> None:
    db = _StubDatabase()
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/notifications")
    assert resp.status_code == 401


async def test_platform_admin_rejected_on_implicit_route() -> None:
    db = _StubDatabase()
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, None, subject="pa-1")
        resp = await client.get("/admin/notifications", cookies={"access_token": token})
    assert resp.status_code == 403


async def test_platform_admin_succeeds_on_tenant_explicit_route() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, None, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_A}/notifications", cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


async def test_platform_admin_unknown_tenant_returns_404() -> None:
    db = _StubDatabase()
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, None, subject="pa-1")
        resp = await client.get(
            "/admin/tenants/unknown-tenant/notifications", cookies={"access_token": token},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The pills filter real data (D4) -- and there is no mentions category
# ---------------------------------------------------------------------------


async def test_category_leads_filter_returns_only_leads() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    db.seed_event(tenant_id=_TENANT_A, event_id="e2", kind="ingestion_failed", category="system")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_A)
        resp = await client.get(
            "/admin/notifications", params={"category": "leads"}, cookies={"access_token": token},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert all(item["category"] == "leads" for item in data["items"])


async def test_category_system_filter_returns_only_system() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    db.seed_event(tenant_id=_TENANT_A, event_id="e2", kind="ingestion_failed", category="system")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_A)
        resp = await client.get(
            "/admin/notifications", params={"category": "system"}, cookies={"access_token": token},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert all(item["category"] == "system" for item in data["items"])


async def test_mentions_category_rejected() -> None:
    """D4: there is no 'mentions' category anywhere in the API -- rejected
    with an explicit validation error, never silently accepted or filtered
    to an always-empty set."""
    db = _StubDatabase()
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_A)
        resp = await client.get(
            "/admin/notifications", params={"category": "mentions"}, cookies={"access_token": token},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_cross_tenant_list_never_returns_other_tenants_events() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    db.seed_event(tenant_id=_TENANT_B, event_id="e2", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_B)
        resp = await client.get("/admin/notifications", cookies={"access_token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["event_id"] == "e2"


async def test_cross_tenant_mark_read_returns_404_no_existence_leak() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_B)
        resp = await client.patch(
            "/admin/notifications/e1/read", json={"read": True}, cookies={"access_token": token},
        )
    assert resp.status_code == 404


async def test_no_response_body_contains_tenant_id() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_A)
        resp = await client.get("/admin/notifications", cookies={"access_token": token})
    assert _TENANT_A not in resp.text


# ---------------------------------------------------------------------------
# Per-user read state (D6 -- the single highest-value test in this sprint)
# ---------------------------------------------------------------------------


async def test_user_x_marking_read_does_not_affect_user_y_via_routes() -> None:
    """The exact route-level proof of D6: two users, same tenant, one event.
    X marks it read; Y must still see it as unread, at the same time."""
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    app = _build_app(db)

    token_x = _token(Role.CLIENT_ADMIN, _TENANT_A, subject="user-x")
    token_y = _token(Role.CLIENT_AGENT, _TENANT_A, subject="user-y")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        before_x = await client.get("/admin/notifications", cookies={"access_token": token_x})
        assert before_x.json()["unread_count"] == 1

        mark = await client.patch(
            "/admin/notifications/e1/read", json={"read": True}, cookies={"access_token": token_x},
        )
        assert mark.status_code == 200

        after_x = await client.get("/admin/notifications", cookies={"access_token": token_x})
        after_y = await client.get("/admin/notifications", cookies={"access_token": token_y})

    assert after_x.json()["unread_count"] == 0
    assert after_y.json()["unread_count"] == 1
    assert after_y.json()["items"][0]["read"] is False


async def test_mark_all_read_only_affects_caller_via_routes() -> None:
    db = _StubDatabase()
    db.seed_event(tenant_id=_TENANT_A, event_id="e1", kind="lead_captured", category="leads")
    db.seed_event(tenant_id=_TENANT_A, event_id="e2", kind="lead_assigned", category="leads")
    app = _build_app(db)

    token_x = _token(Role.CLIENT_ADMIN, _TENANT_A, subject="user-x")
    token_y = _token(Role.CLIENT_AGENT, _TENANT_A, subject="user-y")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            "/admin/notifications/read-all", json={}, cookies={"access_token": token_x},
        )
        assert resp.status_code == 200
        assert resp.json()["marked"] == 2

        after_x = await client.get("/admin/notifications", cookies={"access_token": token_x})
        after_y = await client.get("/admin/notifications", cookies={"access_token": token_y})

    assert after_x.json()["unread_count"] == 0
    assert after_y.json()["unread_count"] == 2


# ---------------------------------------------------------------------------
# Bounds (D7)
# ---------------------------------------------------------------------------


async def test_absurd_limit_clamped_server_side() -> None:
    db = _StubDatabase()
    for i in range(5):
        db.seed_event(tenant_id=_TENANT_A, event_id=f"e{i}", kind="lead_captured", category="leads")
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_A)
        resp = await client.get(
            "/admin/notifications", params={"limit": 100000}, cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.json()["limit"] == 200


# ---------------------------------------------------------------------------
# Honest empty state
# ---------------------------------------------------------------------------


async def test_tenant_with_no_events_returns_200_empty() -> None:
    db = _StubDatabase()
    app = _build_app(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, _TENANT_A)
        resp = await client.get("/admin/notifications", cookies={"access_token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["unread_count"] == 0
