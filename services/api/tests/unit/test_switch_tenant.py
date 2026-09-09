"""Unit tests for POST /auth/switch-tenant (multi-chatbot accounts).

Covers: a successful switch within the caller's own account, tenant
isolation (a tenant in a DIFFERENT account must be unreachable, 404 not
403 -- no cross-account enumeration), RBAC (PLATFORM_ADMIN/VISITOR
rejected; both CLIENT_ADMIN and CLIENT_AGENT allowed), and a disabled
target tenant behaving the same as a nonexistent one.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token, decode_access_token

_TEST_JWT_SECRET = "x" * 48

_ACCOUNT_A = "account-a"
_ACCOUNT_B = "account-b"

_TENANT_A1 = "tenant-a-1"
_TENANT_A2 = "tenant-a-2"
_TENANT_A_DISABLED = "tenant-a-disabled"
_TENANT_B1 = "tenant-b-1"

_USER_A_ADMIN = "user-a-admin"
_USER_A_AGENT = "user-a-agent"

_USERS: dict[str, dict[str, Any]] = {
    _USER_A_ADMIN: {"id": _USER_A_ADMIN, "client_account_id": _ACCOUNT_A, "role": "CLIENT_ADMIN"},
    _USER_A_AGENT: {"id": _USER_A_AGENT, "client_account_id": _ACCOUNT_A, "role": "CLIENT_AGENT"},
}

_TENANTS: dict[str, dict[str, Any]] = {
    _TENANT_A1: {"id": _TENANT_A1, "name": "Chatbot A1", "slug": "chatbot-a1", "enabled": True, "client_account_id": _ACCOUNT_A},
    _TENANT_A2: {"id": _TENANT_A2, "name": "Chatbot A2", "slug": "chatbot-a2", "enabled": True, "client_account_id": _ACCOUNT_A},
    _TENANT_A_DISABLED: {"id": _TENANT_A_DISABLED, "name": "Disabled", "slug": "disabled", "enabled": False, "client_account_id": _ACCOUNT_A},
    _TENANT_B1: {"id": _TENANT_B1, "name": "Chatbot B1", "slug": "chatbot-b1", "enabled": True, "client_account_id": _ACCOUNT_B},
}


class _StubDatabase:
    """Database double serving canned user/tenant rows + recording audit calls."""

    def __init__(self) -> None:
        self.audit_calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        q = query.upper()
        if "FROM USERS" in q and "WHERE ID = $1" in q:
            return dict(_USERS[str(args[0])]) if str(args[0]) in _USERS else None
        if "FROM TENANTS" in q and "WHERE ID = $1" in q:
            return dict(_TENANTS[str(args[0])]) if str(args[0]) in _TENANTS else None
        return None

    async def execute(self, query: str, *args: object) -> str:
        if "AUDIT" in query.upper():
            self.audit_calls.append(args)
        return "INSERT 1"

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


def _build_app(db: Any = None) -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db if db is not None else _StubDatabase()
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _mint_cookie(
    *,
    subject: str,
    role: Role,
    tenant_id: str | None,
    secret: str = _TEST_JWT_SECRET,
) -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=secret, ttl_seconds=300)
    return token


async def test_client_admin_switches_to_another_tenant_in_their_own_account() -> None:
    db = _StubDatabase()
    app = _build_app(db)
    token = _mint_cookie(subject=_USER_A_ADMIN, role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/auth/switch-tenant",
            json={"tenant_id": _TENANT_A2},
            cookies={"access_token": token},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == _TENANT_A2
    assert body["name"] == "Chatbot A2"
    assert body["slug"] == "chatbot-a2"

    # The re-minted cookie's JWT claims the NEW tenant, not the old one.
    cookie_header = resp.headers.get("set-cookie", "")
    new_token = cookie_header.split("access_token=")[1].split(";")[0]
    payload = decode_access_token(new_token, secret=_TEST_JWT_SECRET)
    assert payload["tenant_id"] == _TENANT_A2
    assert payload["sub"] == _USER_A_ADMIN

    assert len(db.audit_calls) == 1


async def test_client_agent_can_also_switch() -> None:
    """CLIENT_AGENT gets the same account-level switcher as CLIENT_ADMIN."""
    app = _build_app()
    token = _mint_cookie(subject=_USER_A_AGENT, role=Role.CLIENT_AGENT, tenant_id=_TENANT_A1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/auth/switch-tenant",
            json={"tenant_id": _TENANT_A2},
            cookies={"access_token": token},
        )

    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == _TENANT_A2


async def test_switching_to_a_tenant_in_a_different_account_returns_404_not_403() -> None:
    """Isolation -- a tenant that genuinely exists, just in ANOTHER account,
    must be indistinguishable from a nonexistent one: 404 TENANT_NOT_FOUND,
    never 403 (which would confirm the target's existence to a probing
    caller)."""
    db = _StubDatabase()
    app = _build_app(db)
    token = _mint_cookie(subject=_USER_A_ADMIN, role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/auth/switch-tenant",
            json={"tenant_id": _TENANT_B1},
            cookies={"access_token": token},
        )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TENANT_NOT_FOUND"
    # Cookie left unmodified -- no re-mint on a rejected switch.
    assert "set-cookie" not in resp.headers
    assert db.audit_calls == []


async def test_switching_to_an_unknown_tenant_id_returns_the_same_404() -> None:
    app = _build_app()
    token = _mint_cookie(subject=_USER_A_ADMIN, role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/auth/switch-tenant",
            json={"tenant_id": "does-not-exist"},
            cookies={"access_token": token},
        )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_switching_to_a_disabled_tenant_in_own_account_returns_404() -> None:
    app = _build_app()
    token = _mint_cookie(subject=_USER_A_ADMIN, role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/auth/switch-tenant",
            json={"tenant_id": _TENANT_A_DISABLED},
            cookies={"access_token": token},
        )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_platform_admin_rejected_403() -> None:
    app = _build_app()
    token = _mint_cookie(subject="pa-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/auth/switch-tenant",
            json={"tenant_id": _TENANT_A1},
            cookies={"access_token": token},
        )

    assert resp.status_code == 403


async def test_visitor_rejected_403() -> None:
    app = _build_app()
    token = _mint_cookie(subject="visitor-1", role=Role.VISITOR, tenant_id=_TENANT_A1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/auth/switch-tenant",
            json={"tenant_id": _TENANT_A2},
            cookies={"access_token": token},
        )

    assert resp.status_code == 403


async def test_no_cookie_returns_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/switch-tenant", json={"tenant_id": _TENANT_A1})

    assert resp.status_code == 401
