"""Unit tests for auth routes (login audit wiring).

Covers:
- A successful login records exactly one auth.login audit event.
- A login where record_audit raises still returns the normal login response
  (audit is non-fatal).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

# -- Constants -----------------------------------------------------------------

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"
_ACCOUNT_ID = "account-abc-123"
_TEST_PASSWORD_HASH = "$pbkdf2-sha256$test-hash"

# -- Test doubles --------------------------------------------------------------


class _StubDatabase:
    """Database double that returns a user row and records audit calls."""

    def __init__(self) -> None:
        self.audit_calls: list[tuple[str, dict[str, Any]]] = []
        self.last_sql: str = ""
        self.last_params: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        self.last_sql = query
        self.last_params = args
        if "users" in query:
            return {
                "id": "user-1",
                "email": "admin@example.com",
                "password_hash": _TEST_PASSWORD_HASH,
                "role": "CLIENT_ADMIN",
                "tenant_id": _TENANT_ID,
                "client_account_id": _ACCOUNT_ID,
                "active": True,
                "name": "Admin User",
            }
        if "tenants" in query:
            return {"id": _TENANT_ID}
        return None

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.last_sql = query
        self.last_params = args
        return []

    async def execute(self, query: str, *args: object) -> str:
        self.last_sql = query
        self.last_params = args
        if "audit" in query:
            self.audit_calls.append((query, dict(zip(
                ["tenant_id", "event_id", "actor", "action", "target_type", "target_id", "metadata"],
                args,
                strict=False,
            ))))
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


# -- Helpers -------------------------------------------------------------------

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
    """Create app with test doubles."""
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
    subject: str = "user-1",
    role: Role = Role.CLIENT_ADMIN,
    tenant_id: str | None = _TENANT_ID,
    ttl_seconds: int = 300,
    secret: str = _TEST_JWT_SECRET,
) -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=secret, ttl_seconds=ttl_seconds)
    return token


# ==============================================================================
# POST /auth/login — audit wiring
# ==============================================================================


async def test_login_records_auth_login_audit_event() -> None:
    """Successful login → records exactly one auth.login audit event."""
    db = _StubDatabase()
    app = _build_app(db=db)

    with patch("api.auth.routes.verify_password", return_value=True):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/auth/login",
                json={"email": "admin@example.com", "password": "correct"},
            )

    assert resp.status_code == 200
    assert len(db.audit_calls) == 1
    sql, params = db.audit_calls[0]
    assert "audit_events" in sql
    assert params["action"] == "auth.login"
    assert params["target_type"] == "user"
    assert params["target_id"] == "user-1"
    assert params["actor"] == "user-1"


async def test_login_succeeds_when_audit_fails() -> None:
    """Login where record_audit raises → still returns normal login response."""
    db = _StubDatabase()
    app = _build_app(db=db)

    # Make the audit insert raise
    original_execute = db.execute

    async def _failing_execute(query: str, *args: object) -> str:
        if "audit" in query:
            raise RuntimeError("audit DB failure")
        return await original_execute(query, *args)

    db.execute = _failing_execute  # type: ignore[method-assign]

    with patch("api.auth.routes.verify_password", return_value=True):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/auth/login",
                json={"email": "admin@example.com", "password": "correct"},
            )

    # Login should still succeed
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "admin@example.com"
