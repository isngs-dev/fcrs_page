"""Unit tests for /admin/accounts (POST/GET list/GET detail) + the
/admin/tenants/{tenant_id}/accounts tenant-explicit twins.

Covers (SR-9.2 D8/D9):
- CLIENT_ADMIN full access (POST/GET list/GET detail).
- CLIENT_AGENT read-only (403 on POST).
- VISITOR 403 on everything.
- No-auth 401.
- PLATFORM_ADMIN 403 on implicit routes; 200 on tenant-explicit routes
  against a valid tenant, 404 TENANT_NOT_FOUND for an unknown tenant_id.
- Cross-tenant GET of another tenant's account_id -> 404 (not 403).
- Response never contains tenant_id.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _StubDatabase:
    """In-memory stub database backing /admin/accounts for these tests."""

    def __init__(self) -> None:
        self._accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self._tenants: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    def seed_tenant(self, *, tenant_id: str, slug: str, enabled: bool = True) -> None:
        self._tenants[tenant_id] = {
            "id": tenant_id, "name": slug, "slug": slug, "enabled": enabled,
        }

    def seed(self, *, tenant_id: str, account_id: str, name: str = "Acme Ltd", domain: str | None = "acme.example") -> None:
        self._accounts[(tenant_id, account_id)] = {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "name": name,
            "domain": domain,
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        if "COUNT(*)" in q and "FROM ACCOUNTS" in q:
            tenant_id = args[0]
            rows = [r for r in self._accounts.values() if r["tenant_id"] == tenant_id]
            return {"count": len(rows)}
        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q:
            tenant_id, account_id = args[0], args[1]
            return self._accounts.get((tenant_id, account_id))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM ACCOUNTS" in q:
            tenant_id = args[0]
            rows = [r for r in self._accounts.values() if r["tenant_id"] == tenant_id]
            rows.sort(key=lambda r: (r["created_at"], r["account_id"]), reverse=True)
            if "LIMIT $" in q:
                limit = args[1]
                offset = args[2] if len(args) > 2 else 0
                rows = rows[offset : offset + limit]
            return rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO ACCOUNTS"):
            tenant_id, account_id, name, domain = args
            self._accounts[(tenant_id, account_id)] = {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "name": name,
                "domain": domain,
                "created_at": _NOW,
                "updated_at": _NOW,
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
    return app


def _token(role: Role, tenant_id: str | None = _TENANT_ID, subject: str = "user-1") -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


@pytest.fixture
def db() -> _StubDatabase:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_tenant(tenant_id=_OTHER_TENANT_ID, slug="widgetco")
    return d


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# POST /admin/accounts
# ---------------------------------------------------------------------------


async def test_client_admin_post_returns_201(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/accounts",
            json={"name": "Acme Ltd", "domain": "acme.example"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Acme Ltd"
    assert data["domain"] == "acme.example"
    assert "tenant_id" not in data


async def test_client_agent_post_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/accounts", json={"name": "Acme Ltd"}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_visitor_post_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post(
            "/admin/accounts", json={"name": "Acme Ltd"}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_no_auth_post_returns_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/accounts", json={"name": "Acme Ltd"})

    assert response.status_code == 401


async def test_platform_admin_post_implicit_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.post(
            "/admin/accounts", json={"name": "Acme Ltd"}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_platform_admin_post_tenant_explicit_returns_201(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="pa-1")
        response = await client.post(
            f"/admin/tenants/{_TENANT_ID}/accounts",
            json={"name": "Acme Ltd"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    rows = [r for r in db.audit_rows if r["action"] == "account_created"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-1"
    assert rows[0]["metadata"]["platform_admin"] is True


async def test_platform_admin_post_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.post(
            "/admin/tenants/does-not-exist/accounts",
            json={"name": "Acme Ltd"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /admin/accounts (list)
# ---------------------------------------------------------------------------


async def test_client_admin_list_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/accounts", cookies={"access_token": token})

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["account_id"] == "acct-1"
    assert "tenant_id" not in data["items"][0]


async def test_client_agent_list_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/accounts", cookies={"access_token": token})

    assert response.status_code == 200


async def test_visitor_list_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/accounts", cookies={"access_token": token})

    assert response.status_code == 403


async def test_no_auth_list_returns_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/accounts")

    assert response.status_code == 401


async def test_platform_admin_list_implicit_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/accounts", cookies={"access_token": token})

    assert response.status_code == 403


async def test_platform_admin_list_tenant_explicit_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts", cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_platform_admin_list_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/tenants/does-not-exist/accounts", cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_list_cross_tenant_isolation(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-mine")
    db.seed(tenant_id=_OTHER_TENANT_ID, account_id="acct-other")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)
        response = await client.get("/admin/accounts", cookies={"access_token": token})

    ids = [item["account_id"] for item in response.json()["items"]]
    assert ids == ["acct-mine"]


# ---------------------------------------------------------------------------
# GET /admin/accounts/{account_id}
# ---------------------------------------------------------------------------


async def test_client_admin_get_detail_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/accounts/acct-1", cookies={"access_token": token})

    assert response.status_code == 200
    data = response.json()
    assert data["account_id"] == "acct-1"
    assert "tenant_id" not in data


async def test_client_agent_get_detail_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/accounts/acct-1", cookies={"access_token": token})

    assert response.status_code == 200


async def test_visitor_get_detail_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/accounts/acct-1", cookies={"access_token": token})

    assert response.status_code == 403


async def test_no_auth_get_detail_returns_401(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/accounts/acct-1")

    assert response.status_code == 401


async def test_get_detail_unknown_id_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/accounts/does-not-exist", cookies={"access_token": token},
        )

    assert response.status_code == 404


async def test_cross_tenant_get_detail_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, tenant_id=_OTHER_TENANT_ID)
        response = await client.get("/admin/accounts/acct-1", cookies={"access_token": token})

    assert response.status_code == 404


async def test_platform_admin_get_detail_implicit_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/accounts/acct-1", cookies={"access_token": token})

    assert response.status_code == 403


async def test_platform_admin_get_detail_tenant_explicit_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts/acct-1", cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["account_id"] == "acct-1"


async def test_platform_admin_get_detail_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/tenants/does-not-exist/accounts/acct-1", cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"
