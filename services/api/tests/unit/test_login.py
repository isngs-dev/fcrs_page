"""Unit tests for POST /auth/login.

Uses httpx ASGITransport -- no live DB needed. The DB is replaced via app.state
with a StubDatabase that returns canned rows for get_user_by_email queries.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from common.cache import InMemoryCache
from common.crypto import hash_password
from httpx import ASGITransport, AsyncClient

# -- Test doubles --------------------------------------------------------------

# Non-secret test value used only in unit tests.
_KNOWN_PASSPHRASE = "correct horse battery staple"
_KNOWN_HASH = hash_password(_KNOWN_PASSPHRASE)

_TENANT_ID = "tenant-abc-123"
_ACCOUNT_ID = "account-abc-123"
_SECOND_TENANT_ID = "tenant-abc-456"  # a 2nd chatbot under the SAME account
_NO_TENANT_ACCOUNT_ID = "account-empty-999"  # an account with zero enabled tenants

_CLIENT_ADMIN_ROW: dict[str, Any] = {
    "id": "ca-user-1",
    "tenant_id": _TENANT_ID,
    "client_account_id": _ACCOUNT_ID,
    "email": "admin@example.com",
    "role": "CLIENT_ADMIN",
    "password_hash": _KNOWN_HASH,
    "name": "Account Owner",
    "active": True,
    "last_login_at": None,
}

_PLATFORM_ADMIN_ROW: dict[str, Any] = {
    "id": "pa-user-1",
    "tenant_id": None,
    "client_account_id": None,
    "email": "platform@chatbot.local",
    "role": "PLATFORM_ADMIN",
    "password_hash": _KNOWN_HASH,
    "name": "Platform Admin",
    "active": True,
    "last_login_at": None,
}

_INACTIVE_ROW: dict[str, Any] = {
    "id": "inactive-1",
    "tenant_id": _TENANT_ID,
    "client_account_id": _ACCOUNT_ID,
    "email": "inactive@example.com",
    "role": "CLIENT_ADMIN",
    "password_hash": _KNOWN_HASH,
    "name": "Inactive User",
    "active": False,
    "last_login_at": None,
}

_NO_TENANT_ROW: dict[str, Any] = {
    "id": "no-tenant-user-1",
    "tenant_id": None,
    "client_account_id": _NO_TENANT_ACCOUNT_ID,
    "email": "no-tenant@example.com",
    "role": "CLIENT_ADMIN",
    "password_hash": _KNOWN_HASH,
    "name": "Orphaned Admin",
    "active": True,
    "last_login_at": None,
}

_USER_DB: dict[str, dict[str, Any]] = {
    "admin@example.com": _CLIENT_ADMIN_ROW,
    "platform@chatbot.local": _PLATFORM_ADMIN_ROW,
    "inactive@example.com": _INACTIVE_ROW,
    "no-tenant@example.com": _NO_TENANT_ROW,
}

# tenants visible to get_default_tenant_id_for_account -- (id, client_account_id,
# enabled, created_at rank -- lower rank = created earlier).
_TENANTS_DB: list[dict[str, Any]] = [
    {"id": _SECOND_TENANT_ID, "client_account_id": _ACCOUNT_ID, "enabled": True, "rank": 2},
    {"id": _TENANT_ID, "client_account_id": _ACCOUNT_ID, "enabled": True, "rank": 1},
]


class _StubDatabase:
    """Database double that serves canned user/tenant rows for auth queries."""

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        q = query.upper()
        if "FROM USERS" in q and "LOWER(EMAIL)" in q:
            email = str(args[0]).lower()
            for key, row in _USER_DB.items():
                if key.lower() == email:
                    return dict(row)
            return None
        if "FROM USERS" in q and "WHERE ID = $1" in q:
            user_id = str(args[0])
            for row in _USER_DB.values():
                if row["id"] == user_id:
                    return dict(row)
            return None
        if "FROM TENANTS" in q and "CLIENT_ACCOUNT_ID = $1 AND ENABLED" in q:
            account_id = str(args[0])
            candidates = [t for t in _TENANTS_DB if t["client_account_id"] == account_id and t["enabled"]]
            if not candidates:
                return None
            earliest = min(candidates, key=lambda t: t["rank"])
            return {"id": earliest["id"]}
        return None

    async def fetchval(self, query: str, *args: object) -> object:
        return 1

    async def execute(self, query: str, *args: object) -> str:
        return "UPDATE 1"

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


# -- Helpers -------------------------------------------------------------------

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


def _build_app() -> Any:
    """Create app with test doubles injected, bypassing the real lifespan.

    Settings cache is rebuilt inside the patched env and left warm so that
    request-time calls to ``get_api_settings()`` return the test values.
    """
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()
        # Cache is now warm with test settings -- do NOT clear.

    app.state.db = _StubDatabase()
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


# -- Login success: CLIENT_ADMIN -----------------------------------------------


async def test_login_success_returns_200_with_profile() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "admin@example.com",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "ca-user-1"
    assert body["email"] == "admin@example.com"
    assert body["role"] == "CLIENT_ADMIN"
    assert body["tenant_id"] == _TENANT_ID
    assert body["name"] == "Account Owner"
    # Must NOT contain token or password_hash
    assert "token" not in body
    assert "password_hash" not in body
    assert "access_token" not in body


async def test_login_success_sets_httponly_cookie() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "admin@example.com",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 200
    cookie_header = resp.headers.get("set-cookie", "")
    assert "access_token=" in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "samesite=lax" in cookie_header.lower()


# -- Login success: PLATFORM_ADMIN (null tenant_id) ----------------------------


async def test_login_platform_admin_has_null_tenant() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "platform@chatbot.local",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "PLATFORM_ADMIN"
    assert body["tenant_id"] is None


async def test_login_platform_admin_token_claims_carry_null_tenant() -> None:
    """The JWT minted for a PLATFORM_ADMIN carries tenant_id=None in claims."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "platform@chatbot.local",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 200
    cookie_header = resp.headers.get("set-cookie", "")
    # Extract the token value from the Set-Cookie header
    token_value = cookie_header.split("access_token=")[1].split(";")[0]
    from api.auth.tokens import decode_access_token
    payload = decode_access_token(token_value, secret="x" * 48)
    assert payload["tenant_id"] is None
    assert payload["role"] == "PLATFORM_ADMIN"


# -- RBAC/tenancy: CLIENT_ADMIN token carries correct tenant_id ----------------


async def test_login_client_admin_token_claims_carry_tenant() -> None:
    """The JWT minted for a CLIENT_ADMIN carries the correct tenant_id."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "admin@example.com",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 200
    cookie_header = resp.headers.get("set-cookie", "")
    token_value = cookie_header.split("access_token=")[1].split(";")[0]
    from api.auth.tokens import decode_access_token
    payload = decode_access_token(token_value, secret="x" * 48)
    assert payload["tenant_id"] == _TENANT_ID
    assert payload["role"] == "CLIENT_ADMIN"


# -- Wrong passphrase -> 401 (no enumeration) ---------------------------------


async def test_wrong_passphrase_returns_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "admin@example.com",
            "password": "wrong passphrase here",
        })
    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "UNAUTHENTICATED"
    # Must NOT set cookie on failure
    assert "set-cookie" not in resp.headers


# -- Unknown email -> 401 (same shape, no enumeration) ------------------------


async def test_unknown_email_returns_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "nobody@nowhere.example",
            "password": "anything",
        })
    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "UNAUTHENTICATED"


async def test_wrong_passphrase_and_unknown_email_same_shape() -> None:
    """Both failures produce the same error_code and message -- no enumeration."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        wrong_pw = await c.post("/auth/login", json={
            "email": "admin@example.com",
            "password": "wrong",
        })
        unknown = await c.post("/auth/login", json={
            "email": "nobody@nowhere.example",
            "password": "anything",
        })
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["error_code"] == unknown.json()["error_code"]
    assert wrong_pw.json()["message"] == unknown.json()["message"]


# -- Inactive user -> 401 -----------------------------------------------------


async def test_inactive_user_returns_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "inactive@example.com",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "UNAUTHENTICATED"


# -- Multi-chatbot accounts: login resolves the account's DEFAULT tenant -----


async def test_login_resolves_earliest_created_enabled_tenant_not_the_newer_one() -> None:
    """The account behind admin@example.com owns TWO chatbots (_TENANT_ID,
    created first, and _SECOND_TENANT_ID, created later) -- login must
    resolve to the EARLIEST one, never the most-recently-created."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "admin@example.com",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == _TENANT_ID
    assert body["tenant_id"] != _SECOND_TENANT_ID


async def test_login_zero_accessible_tenants_returns_401_no_accessible_tenant() -> None:
    """An account with zero enabled tenants must fail cleanly -- never mint a
    claims object with a fabricated/missing tenant_id (no silent fallback)."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/auth/login", json={
            "email": "no-tenant@example.com",
            "password": _KNOWN_PASSPHRASE,
        })
    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "NO_ACCESSIBLE_TENANT"
    assert "set-cookie" not in resp.headers
