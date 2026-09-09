"""Unit tests for POST/GET /admin/tenants/mine -- CLIENT_ADMIN self-service
multi-chatbot creation (multi-chatbot accounts).

Covers: happy-path creation + listing, slug collision, RBAC (only
CLIENT_ADMIN can create; CLIENT_ADMIN + CLIENT_AGENT can list; PLATFORM_ADMIN
and VISITOR rejected from both), and end-to-end tenant isolation across TWO
real accounts -- account A must never see account B's chatbots via
GET /admin/tenants/mine, even when both have multiple tenants.
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

_ACCOUNT_A = "account-a"
_ACCOUNT_B = "account-b"
_USER_A_ADMIN = "user-a-admin"
_USER_A_AGENT = "user-a-agent"
_USER_B_ADMIN = "user-b-admin"

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
    """In-memory multi-account stub: users + tenants, keyed like the real
    schema, so GET /admin/tenants/mine exercises real cross-account
    filtering rather than a single-account happy path."""

    def __init__(self) -> None:
        self._users: dict[str, dict[str, Any]] = {
            _USER_A_ADMIN: {"id": _USER_A_ADMIN, "client_account_id": _ACCOUNT_A, "role": "CLIENT_ADMIN"},
            _USER_A_AGENT: {"id": _USER_A_AGENT, "client_account_id": _ACCOUNT_A, "role": "CLIENT_AGENT"},
            _USER_B_ADMIN: {"id": _USER_B_ADMIN, "client_account_id": _ACCOUNT_B, "role": "CLIENT_ADMIN"},
        }
        self._tenants: dict[str, dict[str, Any]] = {}
        self._slugs: set[str] = set()

    def seed_tenant(self, *, tenant_id: str, slug: str, account_id: str, name: str = "Seeded") -> None:
        self._tenants[tenant_id] = {
            "id": tenant_id, "name": name, "slug": slug, "enabled": True,
            "client_account_id": account_id,
        }
        self._slugs.add(slug)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.upper()
        if "FROM USERS" in q and "WHERE ID = $1" in q:
            row = self._users.get(str(args[0]))
            return dict(row) if row else None
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.upper()
        if "FROM TENANTS" in q and "CLIENT_ACCOUNT_ID = $1" in q:
            account_id = str(args[0])
            return [dict(t) for t in self._tenants.values() if t["client_account_id"] == account_id]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.upper()
        if q.startswith("INSERT INTO TENANTS"):
            tenant_id, name, slug, enabled, account_id = args
            if slug in self._slugs:
                raise asyncpg.UniqueViolationError()
            self._slugs.add(slug)
            self._tenants[tenant_id] = {
                "id": tenant_id, "name": name, "slug": slug, "enabled": enabled,
                "client_account_id": account_id,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE TENANTS SET CLIENT_KEY_HASH"):
            return "UPDATE 1"
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


def _build_app(db: _StubDatabase) -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    from unittest.mock import patch

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _token(subject: str, role: Role, tenant_id: str | None) -> str:
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
# POST /admin/tenants/mine -- happy path + RBAC
# ---------------------------------------------------------------------------


async def test_client_admin_creates_a_second_chatbot_for_own_account(app: Any) -> None:
    token = _token(_USER_A_ADMIN, Role.CLIENT_ADMIN, tenant_id="whatever-active-tenant")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/tenants/mine",
            json={"name": "Second Bot", "slug": "second-bot"},
            cookies={"access_token": token},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Second Bot"
    assert body["slug"] == "second-bot"
    assert body["client_key"].startswith("pk_")
    assert body["tenant_id"]
    assert "admin_user_id" not in body  # no user created


@pytest.mark.parametrize("role", [Role.CLIENT_AGENT, Role.PLATFORM_ADMIN, Role.VISITOR])
async def test_create_own_tenant_rejects_non_client_admin(app: Any, role: Role) -> None:
    subject = _USER_A_AGENT if role == Role.CLIENT_AGENT else "someone"
    tenant_id = None if role == Role.PLATFORM_ADMIN else "some-tenant"
    token = _token(subject, role, tenant_id=tenant_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/tenants/mine",
            json={"name": "Second Bot", "slug": "second-bot"},
            cookies={"access_token": token},
        )

    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_create_own_tenant_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/admin/tenants/mine", json={"name": "Second Bot", "slug": "second-bot"})

    assert resp.status_code == 401


async def test_create_own_tenant_duplicate_slug(app: Any, db: _StubDatabase) -> None:
    db.seed_tenant(tenant_id="existing", slug="taken", account_id=_ACCOUNT_A)
    token = _token(_USER_A_ADMIN, Role.CLIENT_ADMIN, tenant_id="existing")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/tenants/mine",
            json={"name": "Dup", "slug": "taken"},
            cookies={"access_token": token},
        )

    assert resp.json()["error_code"] == "TENANT_SLUG_TAKEN"


# ---------------------------------------------------------------------------
# GET /admin/tenants/mine -- RBAC + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.PLATFORM_ADMIN, Role.VISITOR])
async def test_list_own_tenants_rejects_non_account_roles(app: Any, role: Role) -> None:
    tenant_id = None if role == Role.PLATFORM_ADMIN else "some-tenant"
    token = _token("someone", role, tenant_id=tenant_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/tenants/mine", cookies={"access_token": token})

    assert resp.status_code == 403


async def test_client_admin_and_agent_both_see_the_full_account_chatbot_list(
    app: Any, db: _StubDatabase
) -> None:
    db.seed_tenant(tenant_id="tenant-a-1", slug="a1", account_id=_ACCOUNT_A, name="Chatbot A1")
    db.seed_tenant(tenant_id="tenant-a-2", slug="a2", account_id=_ACCOUNT_A, name="Chatbot A2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        admin_resp = await c.get(
            "/admin/tenants/mine",
            cookies={"access_token": _token(_USER_A_ADMIN, Role.CLIENT_ADMIN, "tenant-a-1")},
        )
        agent_resp = await c.get(
            "/admin/tenants/mine",
            cookies={"access_token": _token(_USER_A_AGENT, Role.CLIENT_AGENT, "tenant-a-1")},
        )

    for resp in (admin_resp, agent_resp):
        assert resp.status_code == 200
        slugs = {t["slug"] for t in resp.json()["tenants"]}
        assert slugs == {"a1", "a2"}


async def test_account_a_never_sees_account_bs_chatbots_even_with_multiple_each(
    app: Any, db: _StubDatabase
) -> None:
    """The highest-value isolation test in this slice."""
    db.seed_tenant(tenant_id="tenant-a-1", slug="a1", account_id=_ACCOUNT_A, name="Chatbot A1")
    db.seed_tenant(tenant_id="tenant-a-2", slug="a2", account_id=_ACCOUNT_A, name="Chatbot A2")
    db.seed_tenant(tenant_id="tenant-b-1", slug="b1", account_id=_ACCOUNT_B, name="Chatbot B1")
    db.seed_tenant(tenant_id="tenant-b-2", slug="b2", account_id=_ACCOUNT_B, name="Chatbot B2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        a_resp = await c.get(
            "/admin/tenants/mine",
            cookies={"access_token": _token(_USER_A_ADMIN, Role.CLIENT_ADMIN, "tenant-a-1")},
        )
        b_resp = await c.get(
            "/admin/tenants/mine",
            cookies={"access_token": _token(_USER_B_ADMIN, Role.CLIENT_ADMIN, "tenant-b-1")},
        )

    a_slugs = {t["slug"] for t in a_resp.json()["tenants"]}
    b_slugs = {t["slug"] for t in b_resp.json()["tenants"]}
    assert a_slugs == {"a1", "a2"}
    assert b_slugs == {"b1", "b2"}
    assert a_slugs.isdisjoint(b_slugs)
    # Cross-account leak check in the raw response bytes, not just parsed set.
    assert "tenant-b-1" not in a_resp.text
    assert "tenant-b-2" not in a_resp.text


async def test_list_own_tenants_empty_when_account_has_no_chatbots(app: Any) -> None:
    token = _token(_USER_A_ADMIN, Role.CLIENT_ADMIN, tenant_id="whatever")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/tenants/mine", cookies={"access_token": token})

    assert resp.status_code == 200
    assert resp.json()["tenants"] == []
