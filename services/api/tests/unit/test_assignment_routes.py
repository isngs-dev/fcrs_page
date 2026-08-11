"""Unit tests for GET/PUT /admin/assignment-config (SR-20 D1/D2) -- the
round-robin toggle's read/write surface backing the Team-members page.

Covers:
- CLIENT_ADMIN GET/PUT both 200; CLIENT_AGENT -> 403 on both (tenant-wide
  config); VISITOR -> 403; no cookie -> 401.
- PUT persists + a follow-up GET reflects it (round-trip).
- An unconfigured tenant's GET returns round_robin_enabled=false (D1's
  default), never a fabricated value.
- PUT writes an audit row.
- PLATFORM_ADMIN 403 implicit / 200 + 404 TENANT_NOT_FOUND tenant-explicit,
  honest audit actor (M11).
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"

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
    def __init__(self) -> None:
        self._configs: dict[str, dict[str, Any]] = {}
        self._tenants: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    def seed_tenant(self, *, tenant_id: str, enabled: bool = True) -> None:
        self._tenants[tenant_id] = {"id": tenant_id, "enabled": enabled}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if q.startswith("SELECT ROUND_ROBIN_ENABLED"):
            return self._configs.get(args[0])
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO TENANT_ASSIGNMENT_CONFIGS"):
            tenant_id, enabled = args
            self._configs[tenant_id] = {
                "round_robin_enabled": enabled,
                "last_assigned_agent_id": self._configs.get(tenant_id, {}).get(
                    "last_assigned_agent_id"
                ),
            }
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
    d.seed_tenant(tenant_id=_TENANT_ID)
    return d


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


async def test_get_unconfigured_tenant_returns_disabled_default(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/assignment-config", cookies={"access_token": token})

    assert response.status_code == 200
    assert response.json()["round_robin_enabled"] is False


async def test_put_then_get_roundtrips(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        put_response = await client.put(
            "/admin/assignment-config",
            json={"round_robin_enabled": True},
            cookies={"access_token": token},
        )
        assert put_response.status_code == 200
        assert put_response.json()["round_robin_enabled"] is True

        get_response = await client.get(
            "/admin/assignment-config", cookies={"access_token": token},
        )

    assert get_response.json()["round_robin_enabled"] is True


async def test_put_client_agent_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/assignment-config",
            json={"round_robin_enabled": True},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_get_client_agent_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/assignment-config", cookies={"access_token": token})

    assert response.status_code == 200


async def test_visitor_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/assignment-config", cookies={"access_token": token})

    assert response.status_code == 403


async def test_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/assignment-config")

    assert response.status_code == 401


async def test_put_writes_audit_row(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.put(
            "/admin/assignment-config",
            json={"round_robin_enabled": True},
            cookies={"access_token": token},
        )

    assert any(r["action"] == "assignment_config_updated" for r in db.audit_rows)


async def test_platform_admin_implicit_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/assignment-config", cookies={"access_token": token})

    assert response.status_code == 403


async def test_platform_admin_tenant_explicit_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/assignment-config", cookies={"access_token": token},
        )

    assert response.status_code == 200


async def test_platform_admin_unknown_tenant_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/tenants/does-not-exist/assignment-config", cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"
