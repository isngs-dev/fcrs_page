"""Unit tests for S12.7 -- platform-admin super-user tenant-explicit access.

Covers the spec's mandatory test list (services/api side):
3. resolve_tenant_scope role gate (RBAC): PLATFORM_ADMIN resolves; CLIENT_ADMIN/
   CLIENT_AGENT/VISITOR -> 403 ROLE_NOT_PERMITTED; no cookie -> 401.
4. Unknown/inactive target tenant -> 404 TENANT_NOT_FOUND.
5. Platform admin reaches tenant X's full surface (read + CLIENT_ADMIN-only write).
6. Cannot touch tenant Y while targeting X (cross-tenant isolation).
7. CLIENT_ADMIN/CLIENT_AGENT gain NO cross-tenant reach through any path.
8. Honest audit on every mutation: real actor + platform_admin marker for a
   platform admin; no marker for a real CLIENT_ADMIN.
9. Raw platform admin still 403s on the existing implicit client-facing routes.
10. Platform-only powers retained (POST /admin/tenants, rotate-key unchanged).
11. Route-registration: the 15 tenant-explicit variants + implicit routes all
    exist.
12. Test-isolation hygiene: no sys.modules manipulation, only cache_clear() in
    setup, matching the rest of this suite's pattern.
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_X = "tenant-x-111"
_TENANT_Y = "tenant-y-222"

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": _TEST_JWT_SECRET,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
    "ORCHESTRATOR_DEFAULT_ANSWER_THRESHOLD": "0.75",
    "ORCHESTRATOR_DEFAULT_ESCALATE_THRESHOLD": "0.25",
    "ORCHESTRATOR_DEFAULT_TURN_CAP": "6",
}


class _StubDatabase:
    """In-memory stub database backing tenants + settings + leads + audit."""

    def __init__(self) -> None:
        self._tenants: dict[str, dict[str, Any]] = {}
        self._bot_settings: dict[str, dict[str, Any]] = {}
        self._leads: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []
        self.executed_sql: list[tuple[str, tuple[Any, ...]]] = []

    def seed_tenant(self, *, tenant_id: str, slug: str, enabled: bool = True) -> None:
        self._tenants[tenant_id] = {
            "id": tenant_id, "name": slug, "slug": slug, "enabled": enabled,
        }

    def seed_lead(self, *, tenant_id: str, lead_id: str, stage: str = "captured") -> None:
        self._leads[(tenant_id, lead_id)] = {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "visitor_id": "visitor-1",
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": None,
            "status": "new",
            "stage": stage,
            "qualification_score": None,
            "consent": {"granted": True},
            "assigned_agent_id": None,
            "source": "widget",
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        self.executed_sql.append((query, args))
        if "FROM TENANTS WHERE ID" in q:
            tenant_id = args[0]
            return self._tenants.get(tenant_id)
        if "FROM TENANT_BOT_SETTINGS" in q:
            tenant_id = args[0]
            return self._bot_settings.get(tenant_id)
        if "FROM TENANT_ORCHESTRATOR_CONFIGS" in q:
            return None
        if "FROM TENANT_LLM_CONFIGS" in q:
            return None
        if "FROM LEADS" in q and "WHERE TENANT_ID" in q and "AND LEAD_ID" in q:
            tenant_id, lead_id = args[0], args[1]
            return self._leads.get((tenant_id, lead_id))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.executed_sql.append((query, args))
        q = query.strip().upper()
        if "FROM AUDIT_EVENTS" in q:
            tenant_id = args[0]
            return [r for r in self.audit_rows if r["tenant_id"] == tenant_id]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed_sql.append((query, args))
        q = query.strip().upper()
        if q.startswith("INSERT INTO TENANT_BOT_SETTINGS"):
            (
                tenant_id,
                greeting,
                business_hours,
                escalation_policy,
                tone,
                launcher_label,
                sidebar_workspace_label,
                dashboard_title,
                bot_name,
                accent_color,
                launcher_position,
                suggested_questions,
            ) = args
            self._bot_settings[tenant_id] = {
                "greeting": greeting, "business_hours": business_hours,
                "escalation_policy": escalation_policy, "tone": tone,
                "launcher_label": launcher_label,
                "sidebar_workspace_label": sidebar_workspace_label,
                "dashboard_title": dashboard_title,
                "bot_name": bot_name,
                "accent_color": accent_color,
                "launcher_position": launcher_position,
                "suggested_questions": suggested_questions,
            }
            return "INSERT 0 1"
        if q.startswith("INSERT INTO AUDIT_EVENTS"):
            tenant_id, event_id, actor, action, target_type, target_id, metadata = args
            self.audit_rows.append({
                "tenant_id": tenant_id, "event_id": event_id, "actor": actor,
                "action": action, "target_type": target_type, "target_id": target_id,
                "metadata": metadata,
            })
            return "INSERT 1"
        if q.startswith("UPDATE LEADS SET STAGE"):
            stage, status_, score, tenant_id, lead_id = args
            row = self._leads.get((tenant_id, lead_id))
            if row is None:
                return "UPDATE 0"
            row["stage"] = stage
            row["status"] = status_
            row["qualification_score"] = score
            return "UPDATE 1"
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


from datetime import UTC, datetime  # noqa: E402

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app(db: _StubDatabase) -> Any:
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
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _token(role: Role, tenant_id: str | None = None, subject: str = "user-1") -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


@pytest.fixture
def db() -> _StubDatabase:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_X, slug="acme")
    d.seed_tenant(tenant_id=_TENANT_Y, slug="widgetco")
    return d


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# 3. resolve_tenant_scope role gate
# ---------------------------------------------------------------------------


async def test_platform_admin_reaches_tenant_x_settings(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/settings", cookies={"access_token": token},
        )
    assert resp.status_code == 200


@pytest.mark.parametrize("role", [Role.CLIENT_ADMIN, Role.CLIENT_AGENT, Role.VISITOR])
async def test_client_role_forbidden_on_tenant_explicit_route(app: Any, role: Role) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(role, tenant_id=_TENANT_X)
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/settings", cookies={"access_token": token},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_no_cookie_on_tenant_explicit_route_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/admin/tenants/{_TENANT_X}/settings")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4. Unknown/inactive target tenant -> 404
# ---------------------------------------------------------------------------


async def test_unknown_target_tenant_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            "/admin/tenants/does-not-exist/settings", cookies={"access_token": token},
        )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_inactive_target_tenant_404(app: Any, db: _StubDatabase) -> None:
    db.seed_tenant(tenant_id="inactive-tenant", slug="disabled-co", enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            "/admin/tenants/inactive-tenant/settings", cookies={"access_token": token},
        )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TENANT_NOT_FOUND"


# ---------------------------------------------------------------------------
# 5. Platform admin reaches tenant X's full surface (read + write)
# ---------------------------------------------------------------------------


async def test_platform_admin_read_binds_tenant_x(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/settings", cookies={"access_token": token},
        )
    assert resp.status_code == 200
    # The tenant_bot_settings lookup was bound to tenant X.
    settings_calls = [
        args for sql, args in db.executed_sql if "FROM TENANT_BOT_SETTINGS" in sql.upper()
    ]
    assert settings_calls
    assert settings_calls[-1][0] == _TENANT_X


async def test_platform_admin_write_binds_tenant_x(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.put(
            f"/admin/tenants/{_TENANT_X}/settings",
            json={
                "greeting": "super-user edit",
                "launcher_label": "Chat with Acme",
                "sidebar_workspace_label": "Acme support",
                "dashboard_title": "Acme hub",
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.json()["greeting"] == "super-user edit"
    assert resp.json()["launcher_label"] == "Chat with Acme"
    assert resp.json()["sidebar_workspace_label"] == "Acme support"
    assert resp.json()["dashboard_title"] == "Acme hub"
    assert db._bot_settings[_TENANT_X]["greeting"] == "super-user edit"
    assert db._bot_settings[_TENANT_X]["launcher_label"] == "Chat with Acme"
    assert db._bot_settings[_TENANT_X]["sidebar_workspace_label"] == "Acme support"
    assert db._bot_settings[_TENANT_X]["dashboard_title"] == "Acme hub"


async def test_platform_admin_workspace_labels_only_change_the_target_tenant(
    app: Any, db: _StubDatabase
) -> None:
    db._bot_settings[_TENANT_Y] = {
        "greeting": None,
        "business_hours": None,
        "escalation_policy": None,
        "tone": None,
        "launcher_label": None,
        "sidebar_workspace_label": "WidgetCo support",
        "dashboard_title": "WidgetCo hub",
        "bot_name": None,
        "accent_color": None,
        "launcher_position": None,
        "suggested_questions": None,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        response = await client.put(
            f"/admin/tenants/{_TENANT_X}/settings",
            json={"sidebar_workspace_label": "Acme support", "dashboard_title": "Acme hub"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert db._bot_settings[_TENANT_X]["sidebar_workspace_label"] == "Acme support"
    assert db._bot_settings[_TENANT_Y]["sidebar_workspace_label"] == "WidgetCo support"
    assert db._bot_settings[_TENANT_Y]["dashboard_title"] == "WidgetCo hub"


async def test_platform_admin_reaches_leads_list_for_tenant_x(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/leads", cookies={"access_token": token},
        )
    assert resp.status_code == 200


async def test_platform_admin_reaches_audit_list_for_tenant_x(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/audit", cookies={"access_token": token},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 6. Cannot touch tenant Y while targeting X (cross-tenant isolation)
# ---------------------------------------------------------------------------


async def test_platform_admin_targeting_x_cannot_reach_tenant_y_lead(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_lead(tenant_id=_TENANT_Y, lead_id="lead-y-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/leads/lead-y-1", cookies={"access_token": token},
        )
    assert resp.status_code == 404


async def test_platform_admin_targeting_x_reaches_tenant_x_lead(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_lead(tenant_id=_TENANT_X, lead_id="lead-x-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/leads/lead-x-1", cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.json()["lead_id"] == "lead-x-1"


# ---------------------------------------------------------------------------
# 7. CLIENT_ADMIN/CLIENT_AGENT gain NO cross-tenant reach through any path
# ---------------------------------------------------------------------------


async def test_client_admin_own_implicit_route_still_works(app: Any) -> None:
    """CLIENT_ADMIN of tenant X keeps using their own implicit /admin/settings."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_X)
        resp = await client.get("/admin/settings", cookies={"access_token": token})
    assert resp.status_code == 200


async def test_client_admin_forced_explicit_url_to_own_tenant_still_403(app: Any) -> None:
    """Even targeting their OWN tenant via the platform-explicit URL is 403 --
    that route family is PLATFORM_ADMIN-only by construction (D3)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_X)
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/settings", cookies={"access_token": token},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_client_admin_forced_explicit_url_to_foreign_tenant_403(app: Any) -> None:
    """CLIENT_ADMIN of tenant X forcing the explicit URL for tenant Y -> 403,
    never leaking tenant Y data."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_X)
        resp = await client.get(
            f"/admin/tenants/{_TENANT_Y}/settings", cookies={"access_token": token},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# 8. Honest audit on every mutation
# ---------------------------------------------------------------------------


async def test_platform_admin_write_records_honest_audit_row(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-real-id")
        resp = await client.put(
            f"/admin/tenants/{_TENANT_X}/settings",
            json={"greeting": "hi"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    rows = [r for r in db.audit_rows if r["tenant_id"] == _TENANT_X]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-real-id"
    assert rows[0]["metadata"]["platform_admin"] is True
    assert rows[0]["metadata"]["platform_admin_role"] == "PLATFORM_ADMIN"


async def test_real_client_admin_write_records_audit_without_marker(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_X, subject="real-admin-1")
        resp = await client.put(
            "/admin/settings", json={"greeting": "hi"}, cookies={"access_token": token},
        )
    assert resp.status_code == 200
    rows = [r for r in db.audit_rows if r["tenant_id"] == _TENANT_X]
    assert len(rows) == 1
    assert rows[0]["actor"] == "real-admin-1"
    assert rows[0]["metadata"] is None or "platform_admin" not in rows[0]["metadata"]


async def test_platform_admin_lead_stage_transition_records_audit(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_lead(tenant_id=_TENANT_X, lead_id="lead-x-2", stage="captured")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.patch(
            f"/admin/tenants/{_TENANT_X}/leads/lead-x-2",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    rows = [r for r in db.audit_rows if r["action"] == "lead_stage_transitioned"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-1"
    assert rows[0]["metadata"]["platform_admin"] is True


# ---------------------------------------------------------------------------
# 9. Raw platform admin still 403s on the existing implicit routes
# ---------------------------------------------------------------------------


async def test_raw_platform_admin_403_on_implicit_settings_route(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get("/admin/settings", cookies={"access_token": token})
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_raw_platform_admin_403_on_implicit_leads_route(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get("/admin/leads", cookies={"access_token": token})
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_raw_platform_admin_403_on_implicit_audit_route(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get("/admin/audit", cookies={"access_token": token})
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# 10. Platform-only powers retained (regression guard)
# ---------------------------------------------------------------------------


async def test_onboard_tenant_still_platform_admin_only(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_X)
        resp = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme-new", "admin_email": "a@acme.test"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_rotate_key_still_platform_admin_only(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_X)
        resp = await client.post(
            f"/admin/tenants/{_TENANT_X}/rotate-key", cookies={"access_token": token},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 11. Route-registration: all 15 tenant-explicit variants exist
# ---------------------------------------------------------------------------


def test_all_fifteen_tenant_explicit_routes_registered() -> None:
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

    schema = app.openapi()
    paths = set(schema["paths"].keys())

    expected = {
        "/admin/tenants/{tenant_id}/settings",
        "/admin/tenants/{tenant_id}/leads",
        "/admin/tenants/{tenant_id}/leads/export",
        "/admin/tenants/{tenant_id}/leads/{lead_id}",
        "/admin/tenants/{tenant_id}/leads/{lead_id}/notes",
        "/admin/tenants/{tenant_id}/leads/{lead_id}/assignment",
        "/admin/tenants/{tenant_id}/leads/{lead_id}/activities",
        "/admin/tenants/{tenant_id}/ingestion/upload",
        "/admin/tenants/{tenant_id}/ingestion/docs/{doc_id}",
        "/admin/tenants/{tenant_id}/analytics/overview",
        "/admin/tenants/{tenant_id}/conversations",
        "/admin/tenants/{tenant_id}/conversations/{conversation_id}",
        "/admin/tenants/{tenant_id}/audit",
    }
    assert expected.issubset(paths)

    # Existing implicit routes are still registered, unchanged.
    implicit_expected = {
        "/admin/settings",
        "/admin/leads",
        "/admin/leads/export",
        "/admin/leads/{lead_id}",
        "/admin/leads/{lead_id}/notes",
        "/admin/leads/{lead_id}/assignment",
        "/admin/leads/{lead_id}/activities",
        "/admin/ingestion/upload",
        "/admin/ingestion/docs/{doc_id}",
        "/admin/analytics/overview",
        "/admin/conversations",
        "/admin/conversations/{conversation_id}",
        "/admin/audit",
        "/admin/tenants",
        "/admin/tenants/{tenant_id}/rotate-key",
    }
    assert implicit_expected.issubset(paths)


# ---------------------------------------------------------------------------
# Leads export ordering under the tenant-explicit router (declaration order)
# ---------------------------------------------------------------------------


async def test_platform_admin_leads_export_not_swallowed_by_lead_id_route(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, subject="pa-1")
        resp = await client.get(
            f"/admin/tenants/{_TENANT_X}/leads/export", cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
