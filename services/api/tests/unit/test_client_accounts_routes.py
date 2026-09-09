"""Unit tests for GET /debug/client-accounts (multi-chatbot accounts,
Slice 6 -- platform-admin `/clients` grouping).

PLATFORM_ADMIN-only by construction (global-only, no per-account isolation
case exists for this route -- there is no non-global caller who could ever
reach it to isolate against). Covers: happy path listing every account,
and RBAC rejection for every other role including a real CLIENT_ADMIN.
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48

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
    def __init__(self, accounts: list[dict[str, Any]] | None = None) -> None:
        self._accounts = accounts or []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM client_accounts" in query:
            return list(self._accounts)
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


def _build_app(db: _StubDatabase | None = None) -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    from unittest.mock import patch

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db if db is not None else _StubDatabase()
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _token(role: Role, tenant_id: str | None) -> str:
    claims = AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


async def test_platform_admin_lists_every_account() -> None:
    db = _StubDatabase(
        accounts=[
            {"id": "account-a", "name": "Acme Roofing", "created_at": None, "updated_at": None},
            {"id": "account-b", "name": "Beta Solar", "created_at": None, "updated_at": None},
        ]
    )
    app = _build_app(db)
    token = _token(Role.PLATFORM_ADMIN, tenant_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/debug/client-accounts", cookies={"access_token": token})

    assert resp.status_code == 200
    body = resp.json()
    assert {row["id"] for row in body} == {"account-a", "account-b"}


@pytest.mark.parametrize("role", [Role.CLIENT_ADMIN, Role.CLIENT_AGENT, Role.VISITOR])
async def test_non_platform_admin_rejected_403(role: Role) -> None:
    app = _build_app()
    token = _token(role, tenant_id="some-tenant")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/debug/client-accounts", cookies={"access_token": token})

    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_no_cookie_returns_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/debug/client-accounts")

    assert resp.status_code == 401
