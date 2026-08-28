"""Unit tests for api.calls.admin_routes (missed-call text-back admin config).

Covers:
- GET/PUT /admin/calls/config: round-trips the config, leak-free (no tenant_id).
- RBAC: CLIENT_ADMIN 200, CLIENT_AGENT 403, no cookie 401 (matches
  test_training_routes.py's "one representative route each" convention).
- Validation: blank message / malformed phone number -> 422, nothing persisted.
- PLATFORM_ADMIN via the tenant-scoped route -> 200; forbidden on the implicit route.
"""
from __future__ import annotations

from typing import Any

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

_TEST_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}

_TENANT_ID = "tenant-calls-1"


class _StubDatabase:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        return self._rows.get(args[0])

    async def execute(self, query: str, *args: Any) -> str:
        tenant_id, monitored_phone_number, enabled, text_back_message = args
        self._rows[tenant_id] = {
            "monitored_phone_number": monitored_phone_number,
            "enabled": enabled,
            "text_back_message": text_back_message,
        }
        return "INSERT 1"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
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


def _build_app() -> Any:
    from unittest.mock import patch

    _reset_settings()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.app import create_app

        app = create_app()
        app.state.db = _StubDatabase()
        app.state.redis = _StubRedis()
        app.state.cache = InMemoryCache()
        app.state.rate_limiter = None
        return app


def _mint_cookie(*, role: Role = Role.CLIENT_ADMIN, tenant_id: str | None = _TENANT_ID) -> str:
    from api.auth.tokens import create_access_token

    claims = AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret="x" * 48, ttl_seconds=300)
    return token


async def test_get_config_returns_honest_unset_state_before_first_save() -> None:
    app = _build_app()
    token = _mint_cookie()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/calls/config", cookies={"access_token": token})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"monitored_phone_number": None, "enabled": False, "text_back_message": None}


async def test_put_then_get_round_trip() -> None:
    app = _build_app()
    token = _mint_cookie()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        put_resp = await c.put(
            "/admin/calls/config",
            cookies={"access_token": token},
            json={
                "monitored_phone_number": "+15005550006",
                "enabled": True,
                "text_back_message": "Sorry we missed your call! Chat here: example.com",
            },
        )
        get_resp = await c.get("/admin/calls/config", cookies={"access_token": token})

    assert put_resp.status_code == 200
    assert put_resp.json() == {
        "monitored_phone_number": "+15005550006",
        "enabled": True,
        "text_back_message": "Sorry we missed your call! Chat here: example.com",
    }
    assert get_resp.json() == put_resp.json()
    assert "tenant_id" not in put_resp.json()


async def test_put_blank_message_422() -> None:
    app = _build_app()
    token = _mint_cookie()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put(
            "/admin/calls/config",
            cookies={"access_token": token},
            json={"monitored_phone_number": "+15005550006", "enabled": True, "text_back_message": "   "},
        )
    assert resp.status_code == 422


async def test_put_malformed_phone_number_422() -> None:
    app = _build_app()
    token = _mint_cookie()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put(
            "/admin/calls/config",
            cookies={"access_token": token},
            json={"monitored_phone_number": "not-a-phone-number", "enabled": True, "text_back_message": "Hi"},
        )
    assert resp.status_code == 422


async def test_client_agent_403() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/calls/config", cookies={"access_token": token})
    assert resp.status_code == 403


async def test_no_cookie_401() -> None:
    app = _build_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/calls/config")
    assert resp.status_code == 401


async def test_platform_admin_forbidden_on_implicit_route() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/calls/config", cookies={"access_token": token})
    assert resp.status_code == 403
