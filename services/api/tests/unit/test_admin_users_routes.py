"""Unit tests for GET/POST /admin/users, PATCH /admin/users/{user_id} (S12.2).

Covers:
- Happy path list/create/deactivate/reactivate as CLIENT_ADMIN.
- POST response has a one-time temp_password, never present in a subsequent GET.
- RBAC negatives: CLIENT_AGENT/VISITOR -> 403, no cookie -> 401.
- Cross-tenant user_id on PATCH -> 404.
- Self-deactivation -> 422 INVALID_TARGET_USER.
- Targeting another CLIENT_ADMIN -> 422 INVALID_TARGET_USER.
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


class _StubDatabase:
    """In-memory stub database backing /admin/users for these tests."""

    def __init__(self) -> None:
        self._users: dict[str, dict[str, Any]] = {}
        self._tenants = {
            _TENANT_ID: {"id": _TENANT_ID, "enabled": True},
            _OTHER_TENANT_ID: {"id": _OTHER_TENANT_ID, "enabled": True},
        }
        self._emails_lower: set[str] = set()
        self._seq = 0

    def seed_user(
        self,
        *,
        user_id: str,
        tenant_id: str,
        email: str = "agent@acme.test",
        role: str = "CLIENT_AGENT",
        active: bool = True,
        name: str | None = "Agent Smith",
    ) -> None:
        self._users[user_id] = {
            "id": user_id,
            "tenant_id": tenant_id,
            "email": email,
            "role": role,
            "password_hash": "hashed",
            "name": name,
            "active": active,
            "last_login_at": None,
            "created_at": None,
        }
        self._emails_lower.add(email.lower())

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM USERS" in q:
            tenant_id = args[0]
            rows = [row for row in self._users.values() if row["tenant_id"] == tenant_id]
            return rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if q.startswith("SELECT") and "FROM TENANTS" in q and "WHERE ID = $1" in q:
            return self._tenants.get(str(args[0]))
        if q.startswith("SELECT") and "FROM USERS" in q and "WHERE ID = $1 AND TENANT_ID" in q:
            user_id, tenant_id = args
            row = self._users.get(user_id)
            if row is None or row["tenant_id"] != tenant_id:
                return None
            return dict(row)
        if q.startswith("UPDATE USERS") and "RETURNING" in q:
            active, user_id, tenant_id = args
            row = self._users.get(user_id)
            if row is None or row["tenant_id"] != tenant_id:
                return None
            row["active"] = active
            return dict(row)
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO USERS"):
            user_id, tenant_id, email, role, _password_hash, name = args
            if email.lower() in self._emails_lower:
                raise asyncpg.UniqueViolationError()
            self._emails_lower.add(email.lower())
            self._users[user_id] = {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": email,
                "role": role,
                "password_hash": _password_hash,
                "name": name,
                "active": True,
                "last_login_at": None,
                "created_at": None,
            }
            return "INSERT 0 1"
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
    return _StubDatabase()


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


async def test_get_users_returns_tenant_users(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, email="a@acme.test")
    db.seed_user(user_id="agent-2", tenant_id=_OTHER_TENANT_ID, email="b@other.test")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/users", cookies={"access_token": token})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == "agent-1"
    assert "password_hash" not in body[0]


async def test_platform_admin_can_list_users_for_the_target_tenant(
    app: Any, db: _StubDatabase
) -> None:
    """The tenant-explicit read path supplies SR-25's platform Leads filter."""
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, email="a@acme.test")
    db.seed_user(user_id="agent-2", tenant_id=_OTHER_TENANT_ID, email="b@other.test")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="platform-1")
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/users", cookies={"access_token": token}
        )

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == ["agent-1"]
    assert "password_hash" not in body[0]


async def test_get_users_client_agent_forbidden(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/users", cookies={"access_token": token})

    assert response.status_code == 403
    assert response.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_get_users_visitor_forbidden(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/users", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_users_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/users")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/users
# ---------------------------------------------------------------------------


async def test_post_user_creates_agent_with_one_time_temp_password(
    app: Any, db: _StubDatabase
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/users",
            json={"email": "new-agent@acme.test", "name": "New Agent"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "CLIENT_AGENT"
    assert body["email"] == "new-agent@acme.test"
    assert body["temp_password"]
    assert len(body["temp_password"]) > 0


async def test_post_user_temp_password_not_in_subsequent_get(
    app: Any, db: _StubDatabase
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.post(
            "/admin/users",
            json={"email": "new-agent@acme.test", "name": "New Agent"},
            cookies={"access_token": token},
        )
        list_response = await client.get("/admin/users", cookies={"access_token": token})

    body = list_response.json()
    assert len(body) == 1
    assert "temp_password" not in body[0]


async def test_post_user_no_role_field_accepted_still_creates_agent(
    app: Any, db: _StubDatabase
) -> None:
    """Even if a caller sends a 'role' field, it's ignored -- the response is
    still CLIENT_AGENT (there's no field on the model to bind it to)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/users",
            json={"email": "sneaky@acme.test", "role": "CLIENT_ADMIN"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    assert response.json()["role"] == "CLIENT_AGENT"


async def test_post_user_duplicate_email_422(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="existing", tenant_id=_TENANT_ID, email="dup@acme.test")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/users",
            json={"email": "dup@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "ADMIN_EMAIL_TAKEN"


async def test_post_user_client_agent_forbidden(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/users",
            json={"email": "x@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_post_user_visitor_forbidden(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post(
            "/admin/users",
            json={"email": "x@acme.test"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_post_user_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/users", json={"email": "x@acme.test"})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /admin/users/{user_id}
# ---------------------------------------------------------------------------


async def test_patch_deactivate_agent_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/users/agent-1",
            json={"active": False},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["active"] is False


async def test_patch_reactivate_agent_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, active=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/users/agent-1",
            json={"active": True},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["active"] is True


async def test_patch_cross_tenant_user_id_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_OTHER_TENANT_ID, active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        response = await client.patch(
            "/admin/users/agent-1",
            json={"active": False},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "USER_NOT_FOUND"


async def test_patch_missing_user_id_returns_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/users/does-not-exist",
            json={"active": False},
            cookies={"access_token": token},
        )

    assert response.status_code == 404


async def test_patch_self_deactivation_returns_422(app: Any, db: _StubDatabase) -> None:
    db.seed_user(
        user_id="admin-1", tenant_id=_TENANT_ID, role="CLIENT_ADMIN", active=True
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, subject="admin-1")
        response = await client.patch(
            "/admin/users/admin-1",
            json={"active": False},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_TARGET_USER"


async def test_patch_targeting_another_client_admin_returns_422(
    app: Any, db: _StubDatabase
) -> None:
    db.seed_user(
        user_id="other-admin", tenant_id=_TENANT_ID, role="CLIENT_ADMIN", active=True
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, subject="admin-1")
        response = await client.patch(
            "/admin/users/other-admin",
            json={"active": False},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_TARGET_USER"


async def test_patch_client_agent_forbidden(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.patch(
            "/admin/users/agent-1",
            json={"active": False},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_patch_visitor_forbidden(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.patch(
            "/admin/users/agent-1",
            json={"active": False},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_patch_no_cookie_401(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch("/admin/users/agent-1", json={"active": False})

    assert response.status_code == 401
