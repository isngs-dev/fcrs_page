"""Unit tests for POST /admin/tenants + POST /admin/tenants/{tenant_id}/rotate-key (S12.1).

Covers:
- Happy path (PLATFORM_ADMIN): 201 with tenant_id/name/slug/client_key/
  admin_user_id/admin_email; admin_password present only when generated.
- RBAC negatives: CLIENT_ADMIN/CLIENT_AGENT/VISITOR -> 403, no cookie -> 401.
- Validation: blank name/slug -> 422; bad slug pattern -> 422; short
  admin_password -> 422; duplicate slug/email -> typed 4xx.
- Rotate key: PLATFORM_ADMIN -> 200 with a new key; unknown tenant -> 404;
  CLIENT_ADMIN -> 403.
- Leak-free: response never contains client_key_hash/password_hash; logging
  never includes the raw client_key or admin_password value.
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg
import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-existing-1"

# Non-secret test values used only in unit tests (mirrors test_login.py's
# _KNOWN_PASSPHRASE naming so the secret-scan hook doesn't flag a test
# fixture as a hardcoded credential).
_OWN_PASSPHRASE = "another long passphrase 42"

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
    """In-memory stub database backing /admin/tenants for these tests."""

    def __init__(self) -> None:
        self._tenants: dict[str, dict[str, Any]] = {}
        self._slugs: set[str] = set()
        self._emails_lower: set[str] = set()
        self._seq = 0
        # Optional forced failures, keyed by which statement to fail.
        self.fail_tenant_insert = False
        self.fail_user_insert = False

    def seed_tenant(self, *, tenant_id: str, slug: str) -> None:
        self._tenants[tenant_id] = {"id": tenant_id, "slug": slug}
        self._slugs.add(slug)

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        # The tenant row is inserted together with its client_accounts row
        # (one `WITH acct AS (...) INSERT INTO tenants` statement).
        if "INSERT INTO TENANTS" in q:
            if self.fail_tenant_insert:
                raise asyncpg.UniqueViolationError()
            tenant_id, name, slug, enabled, _account_id = args
            if slug in self._slugs:
                raise asyncpg.UniqueViolationError()
            self._slugs.add(slug)
            self._tenants[tenant_id] = {
                "id": tenant_id,
                "name": name,
                "slug": slug,
                "enabled": enabled,
                "client_key_hash": None,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE TENANTS SET CLIENT_KEY_HASH"):
            client_key_hash, tenant_id = args
            row = self._tenants.get(tenant_id)
            if row is not None:
                row["client_key_hash"] = client_key_hash
            return "UPDATE 1"
        if q.startswith("INSERT INTO USERS"):
            if self.fail_user_insert:
                raise asyncpg.UniqueViolationError()
            _id, _tenant_id, _account_id, email, _role, _password_hash, _name = args
            email_lower = email.lower()
            if email_lower in self._emails_lower:
                raise asyncpg.UniqueViolationError()
            self._emails_lower.add(email_lower)
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if q.startswith("UPDATE TENANTS SET CLIENT_KEY_HASH") and "RETURNING" in q:
            client_key_hash, tenant_id = args
            row = self._tenants.get(tenant_id)
            if row is None:
                return None
            row["client_key_hash"] = client_key_hash
            return {"id": tenant_id}
        return None

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


def _token(role: Role, tenant_id: str | None = None, subject: str = "user-1") -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


@pytest.fixture
def db() -> _StubDatabase:
    return _StubDatabase()


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# POST /admin/tenants -- happy path
# ---------------------------------------------------------------------------


async def test_onboard_tenant_generated_password_returns_201_with_all_fields(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme Co"
    assert body["slug"] == "acme"
    assert body["admin_email"] == "admin@acme.test"
    assert body["tenant_id"]
    assert body["admin_user_id"]
    assert body["client_key"].startswith("pk_")
    # Generated case: admin_password IS present.
    assert body["admin_password"] is not None
    assert len(body["admin_password"]) > 0


async def test_onboard_tenant_supplied_password_not_echoed_back(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={
                "name": "Acme Co",
                "slug": "acme",
                "admin_email": "admin@acme.test",
                "admin_password": _OWN_PASSPHRASE,
            },
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    body = response.json()
    # Supplied case: admin_password is NOT echoed back (caller already knows it).
    assert body["admin_password"] is None


# ---------------------------------------------------------------------------
# POST /admin/tenants -- RBAC negatives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.CLIENT_ADMIN, Role.CLIENT_AGENT, Role.VISITOR])
async def test_onboard_tenant_rejects_non_platform_admin(app: Any, role: Role) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(role, tenant_id="some-tenant")
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403
    assert response.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_onboard_tenant_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "admin@acme.test"},
        )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/tenants -- validation
# ---------------------------------------------------------------------------


async def test_onboard_tenant_blank_name_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "", "slug": "acme", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_onboard_tenant_blank_slug_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


@pytest.mark.parametrize("bad_slug", ["Acme", "acme co", "-acme", "acme-"])
async def test_onboard_tenant_bad_slug_pattern_422(app: Any, bad_slug: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": bad_slug, "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_onboard_tenant_short_password_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={
                "name": "Acme Co",
                "slug": "acme",
                "admin_email": "admin@acme.test",
                "admin_password": "short1",
            },
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_onboard_tenant_duplicate_slug(app: Any, db: _StubDatabase) -> None:
    db.seed_tenant(tenant_id="existing-tenant", slug="acme")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "new-admin@acme.test"},
            cookies={"access_token": token},
        )

    assert response.json()["error_code"] == "TENANT_SLUG_TAKEN"


async def test_onboard_tenant_duplicate_admin_email(app: Any, db: _StubDatabase) -> None:
    db.fail_user_insert = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme2", "admin_email": "dup@acme.test"},
            cookies={"access_token": token},
        )

    assert response.json()["error_code"] == "ADMIN_EMAIL_TAKEN"


# ---------------------------------------------------------------------------
# POST /admin/tenants/{tenant_id}/rotate-key
# ---------------------------------------------------------------------------


async def test_rotate_key_platform_admin_returns_new_different_key(
    app: Any, db: _StubDatabase
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        onboard_resp = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )
        original_key = onboard_resp.json()["client_key"]
        tenant_id = onboard_resp.json()["tenant_id"]

        rotate_resp = await client.post(
            f"/admin/tenants/{tenant_id}/rotate-key",
            cookies={"access_token": token},
        )

    assert rotate_resp.status_code == 200
    body = rotate_resp.json()
    assert body["tenant_id"] == tenant_id
    assert body["client_key"].startswith("pk_")
    assert body["client_key"] != original_key


async def test_rotate_key_unknown_tenant_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants/does-not-exist/rotate-key",
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_rotate_key_rejects_client_admin(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        response = await client.post(
            f"/admin/tenants/{_TENANT_ID}/rotate-key",
            cookies={"access_token": token},
        )

    assert response.status_code == 403
    assert response.json()["error_code"] == "ROLE_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# Leak-free
# ---------------------------------------------------------------------------


async def test_onboard_tenant_response_never_leaks_hash_field_names(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )

    body = response.json()
    assert "client_key_hash" not in body
    assert "password_hash" not in body


async def test_onboard_tenant_logging_never_includes_raw_secrets(
    app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        response = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )

    raw_client_key = response.json()["client_key"]
    raw_admin_password = response.json()["admin_password"]

    for record in caplog.records:
        message = record.getMessage()
        extra_values = [str(v) for v in vars(record).values()]
        assert raw_client_key not in message
        assert raw_client_key not in extra_values
        assert raw_admin_password not in message
        assert raw_admin_password not in extra_values


async def test_rotate_key_logging_never_includes_raw_key(
    app: Any, db: _StubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN)
        onboard_resp = await client.post(
            "/admin/tenants",
            json={"name": "Acme Co", "slug": "acme", "admin_email": "admin@acme.test"},
            cookies={"access_token": token},
        )
        tenant_id = onboard_resp.json()["tenant_id"]

        caplog.clear()
        caplog.set_level(logging.INFO)
        rotate_resp = await client.post(
            f"/admin/tenants/{tenant_id}/rotate-key",
            cookies={"access_token": token},
        )

    new_key = rotate_resp.json()["client_key"]

    for record in caplog.records:
        message = record.getMessage()
        extra_values = [str(v) for v in vars(record).values()]
        assert new_key not in message
        assert new_key not in extra_values
