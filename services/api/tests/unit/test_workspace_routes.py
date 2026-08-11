"""Unit tests for GET/PUT /admin/workspace + the /admin/tenants/{tenant_id}/workspace
tenant-explicit twin (SR-20 D5, M11's paired-router + shared-_impl pattern).

Covers:
- CLIENT_ADMIN GET/PUT both 200; CLIENT_AGENT GET 200, PUT 403 (tenant-wide
  config -- an agent cannot change it, mirrors CLAUDE.md §3).
- VISITOR/no-cookie -> 403/401.
- PUT persists + a follow-up GET reflects it (round-trip).
- PUT an invalid IANA timezone -> 422 INVALID_TIMEZONE, in Python before
  the DB.
- PUT a slug another tenant already holds -> 422 WORKSPACE_SLUG_TAKEN, never
  a 500.
- An unconfigured tenant's GET returns the platform default timezone, never
  null and never a guessed value.
- PLATFORM_ADMIN: 403 on the implicit route; 200 + 404 TENANT_NOT_FOUND +
  honest audit actor on the tenant-explicit twin (M11).
- PUT writes an audit row.
"""
from __future__ import annotations

from typing import Any

import asyncpg
import pytest
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


class _StubDatabase:
    """In-memory stub database backing /admin/workspace for these tests."""

    def __init__(self) -> None:
        self._tenants: dict[str, dict[str, Any]] = {}
        self._slugs: set[str] = set()
        self.audit_rows: list[dict[str, Any]] = []

    def seed_tenant(
        self, *, tenant_id: str, name: str = "Acme", slug: str = "acme",
        timezone: str | None = None, enabled: bool = True,
    ) -> None:
        self._tenants[tenant_id] = {
            "id": tenant_id, "name": name, "slug": slug, "timezone": timezone,
            "enabled": enabled,
        }
        self._slugs.add(slug)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if q.startswith("SELECT NAME, SLUG, TIMEZONE FROM TENANTS"):
            tenant_id = args[0]
            row = self._tenants.get(tenant_id)
            return {"name": row["name"], "slug": row["slug"], "timezone": row["timezone"]} if row else None
        if q.startswith("UPDATE TENANTS SET NAME"):
            name, slug, timezone, tenant_id = args
            if tenant_id not in self._tenants:
                return None
            if slug in self._slugs and slug != self._tenants[tenant_id]["slug"]:
                raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
            self._slugs.discard(self._tenants[tenant_id]["slug"])
            self._tenants[tenant_id].update({"name": name, "slug": slug, "timezone": timezone})
            self._slugs.add(slug)
            return {"name": name, "slug": slug, "timezone": timezone}
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO AUDIT_EVENTS"):
            (tenant_id, event_id, actor, action, target_type, target_id, metadata) = args
            self.audit_rows.append({
                "tenant_id": tenant_id, "event_id": event_id, "actor": actor,
                "action": action, "target_type": target_type, "target_id": target_id,
                "metadata": metadata,
            })
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


def _token(role: Role, tenant_id: str | None = _TENANT_ID, subject: str = "admin-1") -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


@pytest.fixture
def db() -> _StubDatabase:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, name="Acme", slug="acme")
    return d


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# GET /admin/workspace
# ---------------------------------------------------------------------------


async def test_get_workspace_client_admin_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/workspace", cookies={"access_token": token})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Acme"
    assert body["slug"] == "acme"


async def test_get_workspace_unconfigured_timezone_returns_platform_default(
    app: Any, db: _StubDatabase
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/workspace", cookies={"access_token": token})

    body = response.json()
    assert body["timezone"] == "UTC"
    assert body["timezone"] is not None


async def test_get_workspace_client_agent_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/workspace", cookies={"access_token": token})

    assert response.status_code == 200


async def test_get_workspace_visitor_forbidden(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/workspace", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_workspace_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/workspace")

    assert response.status_code == 401


async def test_get_workspace_platform_admin_implicit_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/workspace", cookies={"access_token": token})

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# PUT /admin/workspace
# ---------------------------------------------------------------------------


async def test_put_workspace_client_admin_200_and_roundtrips(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        put_response = await client.put(
            "/admin/workspace",
            json={"name": "New Name", "slug": "new-slug", "timezone": "Europe/London"},
            cookies={"access_token": token},
        )
        assert put_response.status_code == 200
        assert put_response.json()["timezone"] == "Europe/London"

        get_response = await client.get("/admin/workspace", cookies={"access_token": token})

    body = get_response.json()
    assert body["name"] == "New Name"
    assert body["slug"] == "new-slug"
    assert body["timezone"] == "Europe/London"


async def test_put_workspace_invalid_timezone_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/workspace",
            json={"name": "Acme", "slug": "acme", "timezone": "GMT+0"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_TIMEZONE"


async def test_put_workspace_slug_collision_returns_422_not_500(app: Any, db: _StubDatabase) -> None:
    db.seed_tenant(tenant_id=_OTHER_TENANT_ID, name="Other", slug="taken")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/workspace",
            json={"name": "Acme", "slug": "taken", "timezone": None},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "WORKSPACE_SLUG_TAKEN"


async def test_put_workspace_client_agent_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/workspace",
            json={"name": "Acme", "slug": "acme", "timezone": None},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_put_workspace_visitor_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.put(
            "/admin/workspace",
            json={"name": "Acme", "slug": "acme", "timezone": None},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_put_workspace_writes_audit_row(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.put(
            "/admin/workspace",
            json={"name": "Acme", "slug": "acme", "timezone": "Europe/London"},
            cookies={"access_token": token},
        )

    assert any(r["action"] == "workspace_updated" for r in db.audit_rows)


# ---------------------------------------------------------------------------
# /admin/tenants/{tenant_id}/workspace (PLATFORM_ADMIN tenant-explicit twin, M11)
# ---------------------------------------------------------------------------


async def test_platform_admin_get_tenant_explicit_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/workspace", cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Acme"


async def test_platform_admin_get_unknown_tenant_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/tenants/does-not-exist/workspace", cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_platform_admin_put_tenant_explicit_200_honest_audit_actor(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="pa-real-id")
        response = await client.put(
            f"/admin/tenants/{_TENANT_ID}/workspace",
            json={"name": "Renamed", "slug": "acme", "timezone": None},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    rows = [r for r in db.audit_rows if r["action"] == "workspace_updated"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-real-id"
    assert rows[0]["metadata"]["platform_admin"] is True


async def test_platform_admin_put_unknown_tenant_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.put(
            "/admin/tenants/does-not-exist/workspace",
            json={"name": "X", "slug": "x", "timezone": None},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"
