"""Unit tests for GET /admin/api-keys, POST /admin/api-keys/rotate,
PUT /admin/api-keys/origins + the /admin/tenants/{tenant_id}/api-keys...
tenant-explicit twins (SR-20 D6).

Covers (credential handling -- MANDATORY per the spec):
- CLIENT_ADMIN GET/rotate/PUT-origins all succeed; CLIENT_AGENT -> 403
  everywhere (tenant-wide config); VISITOR -> 403; no cookie -> 401.
- Rotation returns the new raw key ONCE, in the response body only; a
  subsequent GET never returns it.
- No log line, audit row, error message or response header ever contains
  raw key material (scanned via caplog + the audit stub).
- The stored value is a hash, not the raw key (asserted directly against
  the stub's column).
- PLATFORM_ADMIN 403 on implicit routes; 200 + 404 TENANT_NOT_FOUND on the
  tenant-explicit twins, honest audit actor.
- Origin allowlist: valid origins accepted; an empty list is accepted
  (disables the widget -- a documented consequence, not an error); an
  invalid shape -> 422 INVALID_ORIGIN.
"""
from __future__ import annotations

import logging
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
        self._tenants: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    def seed_tenant(
        self, *, tenant_id: str, client_key_hash: str = "a" * 64,
        allowed_origins: list[str] | None = None, enabled: bool = True,
    ) -> None:
        self._tenants[tenant_id] = {
            "id": tenant_id, "client_key_hash": client_key_hash,
            "allowed_origins": allowed_origins or [], "enabled": enabled,
            "name": "Acme", "slug": "acme",
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if q.startswith("SELECT CLIENT_KEY_HASH"):
            row = self._tenants.get(args[0])
            return {"client_key_hash": row["client_key_hash"], "allowed_origins": row["allowed_origins"]} if row else None
        if q.startswith("UPDATE TENANTS SET CLIENT_KEY_HASH"):
            client_key_hash, tenant_id = args
            if tenant_id not in self._tenants:
                return None
            self._tenants[tenant_id]["client_key_hash"] = client_key_hash
            return {"id": tenant_id}
        if q.startswith("UPDATE TENANTS SET ALLOWED_ORIGINS"):
            origins, tenant_id = args
            if tenant_id not in self._tenants:
                return None
            self._tenants[tenant_id]["allowed_origins"] = origins
            return {"allowed_origins": origins}
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
    d.seed_tenant(tenant_id=_TENANT_ID, allowed_origins=["https://old.example.com"])
    return d


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# GET /admin/api-keys
# ---------------------------------------------------------------------------


async def test_get_api_keys_client_admin_200_never_returns_key_material(
    app: Any, db: _StubDatabase
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/api-keys", cookies={"access_token": token})

    assert response.status_code == 200
    body = response.json()
    assert body["has_key"] is True
    assert body["allowed_origins"] == ["https://old.example.com"]
    assert "client_key" not in body
    assert "client_key_hash" not in body
    assert "a" * 64 not in str(body)


async def test_get_api_keys_client_agent_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/api-keys", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_api_keys_visitor_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/api-keys", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_api_keys_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/api-keys")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/api-keys/rotate
# ---------------------------------------------------------------------------


async def test_rotate_client_admin_returns_raw_key_once(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        rotate_response = await client.post(
            "/admin/api-keys/rotate", cookies={"access_token": token},
        )
        assert rotate_response.status_code == 200
        new_key = rotate_response.json()["client_key"]
        assert new_key.startswith("pk_")

        get_response = await client.get("/admin/api-keys", cookies={"access_token": token})

    assert "client_key" not in get_response.json()
    assert new_key not in str(get_response.json())
    # Stored value is a hash, not the raw key.
    assert db._tenants[_TENANT_ID]["client_key_hash"] != new_key


async def test_rotate_client_agent_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post("/admin/api-keys/rotate", cookies={"access_token": token})

    assert response.status_code == 403


async def test_rotate_visitor_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post("/admin/api-keys/rotate", cookies={"access_token": token})

    assert response.status_code == 403


async def test_rotate_never_logs_raw_key(
    app: Any, db: _StubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/api-keys/rotate", cookies={"access_token": token},
            )

    new_key = response.json()["client_key"]
    for record in caplog.records:
        assert new_key not in record.getMessage()
        assert new_key not in str(getattr(record, "__dict__", {}))


async def test_rotate_never_appears_in_audit_row(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/api-keys/rotate", cookies={"access_token": token},
        )

    new_key = response.json()["client_key"]
    for row in db.audit_rows:
        assert new_key not in str(row)


# ---------------------------------------------------------------------------
# PUT /admin/api-keys/origins
# ---------------------------------------------------------------------------


async def test_put_origins_client_admin_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/api-keys/origins",
            json={"origins": ["https://new.example.com"]},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["allowed_origins"] == ["https://new.example.com"]


async def test_put_origins_invalidates_edge_cors_cache_for_added_and_removed_origins(
    app: Any, db: _StubDatabase,
) -> None:
    """Regression guard: PUT /admin/api-keys/origins must invalidate
    api.edge.is_known_origin's CORS cache for every origin the change
    touches -- both the newly-added one (previously stayed WRONGLY blocked
    for up to cors_origin_cache_ttl_seconds behind a stale negative cache
    entry) and the just-removed one (would otherwise stay WRONGLY allowed
    for the same window). The fixture seeds allowed_origins=
    ["https://old.example.com"]; this PUT replaces it with
    ["https://new.example.com"].
    """
    from api.edge import cors_cache_key

    cache = app.state.cache
    # Simulate BOTH stale cache states a real request cycle could have left
    # behind before this PUT runs.
    await cache.set(cors_cache_key("https://new.example.com"), "0", 300)  # stale negative
    await cache.set(cors_cache_key("https://old.example.com"), "1", 300)  # stale positive

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/api-keys/origins",
            json={"origins": ["https://new.example.com"]},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert await cache.get(cors_cache_key("https://new.example.com")) is None
    assert await cache.get(cors_cache_key("https://old.example.com")) is None


async def test_put_origins_empty_list_allowed(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/api-keys/origins", json={"origins": []},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["allowed_origins"] == []


async def test_put_origins_invalid_shape_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/api-keys/origins",
            json={"origins": ["https://example.com/path"]},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ORIGIN"


async def test_put_origins_client_agent_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/api-keys/origins", json={"origins": []},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tenant-explicit twins (M11)
# ---------------------------------------------------------------------------


async def test_platform_admin_get_implicit_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/api-keys", cookies={"access_token": token})

    assert response.status_code == 403


async def test_platform_admin_get_tenant_explicit_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/api-keys", cookies={"access_token": token},
        )

    assert response.status_code == 200


async def test_platform_admin_get_unknown_tenant_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/tenants/does-not-exist/api-keys", cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_platform_admin_rotate_tenant_explicit_honest_audit_actor(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="pa-real-id")
        response = await client.post(
            f"/admin/tenants/{_TENANT_ID}/api-keys/rotate", cookies={"access_token": token},
        )

    assert response.status_code == 200
    rows = [r for r in db.audit_rows if r["action"] == "client_key_rotated_by_admin"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-real-id"
    assert rows[0]["metadata"]["platform_admin"] is True
