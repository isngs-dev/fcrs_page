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

SR-29 additions: real ``?sort=``/``?dir=``/``?q=`` on ``GET /admin/accounts``
and its tenant-scoped twin. Ordering *correctness* is proven only against
real Postgres (``test_crm_list_sort_integration.py``) -- these tests own
contract/validation/RBAC and *which rows* come back, per D-TEST.
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

    def _filtered_accounts(
        self, query: str, args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        """Apply WHERE/ORDER BY/LIMIT the way the real repository emits them.

        Hard-fails (``AssertionError``) on any ORDER BY or WHERE fragment it
        does not recognize, rather than silently ignoring it -- SR-29 F3/8a:
        a stub that guesses converts a loud failure into a quiet wrong answer.
        """
        q = query.strip().upper()
        tenant_id = args[0]
        rows = [r for r in self._accounts.values() if r["tenant_id"] == tenant_id]
        idx = 1

        if "NAME ILIKE $" in q and "DOMAIN ILIKE $" in q:
            pattern = str(args[idx])
            idx += 2
            needle = (
                pattern.removeprefix("%").removesuffix("%")
                .replace("\\\\", "\\").replace("\\%", "%").replace("\\_", "_")
                .lower()
            )
            rows = [
                row for row in rows
                if needle in (row["name"] or "").lower()
                or needle in (row["domain"] or "").lower()
            ]

        if "ORDER BY " not in q:
            return rows, len(rows)

        if "ORDER BY NAME " in q:
            value_for = lambda row: row["name"]  # noqa: E731
        elif "ORDER BY DOMAIN " in q:
            value_for = lambda row: row["domain"]  # noqa: E731
        elif "ORDER BY CREATED_AT " in q:
            value_for = lambda row: row["created_at"]  # noqa: E731
        else:
            raise AssertionError(f"stub cannot honor ORDER BY: {query}")

        descending = " DESC NULLS LAST" in q
        non_null_rows = [row for row in rows if value_for(row) is not None]
        null_rows = [row for row in rows if value_for(row) is None]
        non_null_rows.sort(key=lambda row: row["account_id"], reverse=True)
        non_null_rows.sort(key=value_for, reverse=descending)
        null_rows.sort(key=lambda row: row["account_id"], reverse=True)
        rows = [*non_null_rows, *null_rows]
        total = len(rows)

        if "LIMIT $" in q:
            limit = args[idx]
            idx += 1
            offset = args[idx] if idx < len(args) else 0
            rows = rows[offset : offset + limit]

        return rows, total

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        if "COUNT(*)" in q and "FROM ACCOUNTS" in q:
            rows, total = self._filtered_accounts(query, args)
            return {"count": total}
        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q and "ACCOUNT_ID = $" in q:
            tenant_id, account_id = args[0], args[1]
            return self._accounts.get((tenant_id, account_id))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM ACCOUNTS" in q:
            rows, _ = self._filtered_accounts(query, args)
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


# ---------------------------------------------------------------------------
# SR-29: list sort + combined name/domain search contract
# ---------------------------------------------------------------------------


async def test_list_accounts_sort_omitted_preserves_created_desc_default(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        "created_at; DROP TABLE accounts--",
        "created_at) --",
        "name, tenant_id",
        "(SELECT tenant_id)",
        "created_at DESC; SELECT * FROM users",
        "tenant_id",
        "1",
        "name/**/",
        "",
        "NAME",
    ],
)
async def test_list_accounts_sort_sql_injection_payload_returns_422(
    app: Any, db: _StubDatabase, payload: str,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": payload},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_sort_unknown_key_returns_422_invalid_account_sort(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "industry"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_sort_tenant_id_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "tenant_id"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_dir_unknown_returns_422_invalid_account_sort_direction(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name", "dir": "sideways"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT_DIRECTION"


@pytest.mark.parametrize("sort", ["name", "domain", "created"])
async def test_list_accounts_all_three_sort_keys_return_200(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id=f"acct-{sort}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_accounts_sort_with_limit_offset_echoes_clamped_values(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name", "limit": 500, "offset": -5},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 200
    assert data["offset"] == 0


async def test_list_accounts_q_matches_name_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Acme Holdings", domain="other.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "acme"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [i["account_id"] for i in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_q_matches_domain_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Widget Co", domain="acme.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "ACME"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [i["account_id"] for i in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_q_matches_either_field_ored(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-name", name="Findme Inc", domain="x.example")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-domain", name="Widget Co", domain="findme.example")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-none", name="Other Co", domain="other.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "findme"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    ids = {i["account_id"] for i in response.json()["items"]}
    assert ids == {"acct-name", "acct-domain"}


async def test_list_accounts_q_too_short_returns_422_invalid_account_search(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "a"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SEARCH"


async def test_list_accounts_q_too_long_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "a" * 201},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SEARCH"


async def test_list_accounts_q_empty_string_treated_as_omitted_returns_200(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": ""}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


async def test_list_accounts_q_whitespace_only_treated_as_omitted(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "   "}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


async def test_list_accounts_q_percent_wildcard_is_literal_not_match_all(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="100% Match Co")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-2", name="No Percent Co")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "100%"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert [i["account_id"] for i in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_q_underscore_wildcard_is_literal(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="A_B Co")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-2", name="AxB Co")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "A_B"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert [i["account_id"] for i in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_q_changes_total_count(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Acme")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-2", name="Widget")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "acme"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.json()["total"] == 1


async def test_list_accounts_sort_does_not_change_total_count(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.json()["total"] == 2


@pytest.mark.parametrize("sort", ["name", "domain", "created"])
async def test_list_accounts_sort_cross_tenant_isolation_every_sort_key(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    # Tenant B holds the row that would sort first for this key, ascending.
    db.seed(tenant_id=_TENANT_ID, account_id="acct-mine", name="Zzz Mine", domain="zzz.example")
    db.seed(tenant_id=_OTHER_TENANT_ID, account_id="acct-other", name="AAA Other", domain="aaa.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    ids = [i["account_id"] for i in response.json()["items"]]
    assert ids == ["acct-mine"]


async def test_list_accounts_q_cross_tenant_isolation(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-mine", name="Needle Corp")
    db.seed(tenant_id=_OTHER_TENANT_ID, account_id="acct-other", name="Needle Corp")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "needle"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    ids = [i["account_id"] for i in response.json()["items"]]
    assert ids == ["acct-mine"]


async def test_list_accounts_sort_cross_tenant_isolation_with_injection_payload(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-mine")
    db.seed(tenant_id=_OTHER_TENANT_ID, account_id="acct-other")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "tenant_id; DROP TABLE accounts--"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 422


async def test_list_accounts_client_agent_may_sort_and_search(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Acme")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name", "dir": "asc", "q": "acme"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_accounts_visitor_still_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name"},
            cookies={"access_token": _token(Role.VISITOR)},
        )

    assert response.status_code == 403


async def test_list_accounts_no_auth_still_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/accounts", params={"sort": "name"})

    assert response.status_code == 401


async def test_list_accounts_platform_admin_implicit_route_still_403(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/accounts", params={"sort": "name"}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_list_accounts_tenant_scoped_route_accepts_sort_and_q(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Acme")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts",
            params={"sort": "name", "dir": "asc", "q": "acme"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert [i["account_id"] for i in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_tenant_scoped_route_sort_unknown_key_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts",
            params={"sort": "tenant_id"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_tenant_scoped_route_q_too_short_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts",
            params={"q": "a"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SEARCH"


async def test_list_accounts_existing_pagination_behavior_unchanged(
    app: Any, db: _StubDatabase,
) -> None:
    for i in range(3):
        db.seed(tenant_id=_TENANT_ID, account_id=f"acct-{i}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"limit": 2, "offset": 1},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 2
    assert data["offset"] == 1
    assert len(data["items"]) == 2


async def test_list_accounts_response_shape_has_no_new_fields(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/accounts", cookies={"access_token": _token(Role.CLIENT_ADMIN)})

    item = response.json()["items"][0]
    assert set(item.keys()) == {"account_id", "name", "domain", "created_at"}


# ---------------------------------------------------------------------------
# SR-29: GET /admin/accounts -- sort / search
# ---------------------------------------------------------------------------


async def test_list_accounts_sort_omitted_preserves_created_desc_default(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        "created_at; DROP TABLE accounts--",
        "created_at) --",
        "name, tenant_id",
        "(SELECT tenant_id)",
        "created_at DESC; SELECT * FROM users",
        "tenant_id",
        "1",
        "name/**/",
        "",
        "NAME",
    ],
)
async def test_list_accounts_sort_unknown_key_returns_422_invalid_account_sort(
    app: Any, db: _StubDatabase, payload: str,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": payload},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_sort_sql_injection_payload_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "created_at; DROP TABLE accounts--"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_dir_unknown_returns_422_invalid_account_sort_direction(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name", "dir": "sideways"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT_DIRECTION"


@pytest.mark.parametrize("sort", ["name", "domain", "created"])
async def test_list_accounts_all_three_sort_keys_return_200(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id=f"acct-sort-{sort}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_accounts_sort_with_limit_offset_echoes_clamped_values(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name", "limit": 500, "offset": -5},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 200
    assert data["offset"] == 0


async def test_list_accounts_q_matches_name_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Acme Holdings Ltd", domain="other.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "ACME"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [item["account_id"] for item in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_q_matches_domain_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Widget Co", domain="acme.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "ACME"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [item["account_id"] for item in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_q_matches_either_field_ored(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-name-match", name="Needle Corp", domain="other.example")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-domain-match", name="Widget Co", domain="needle.example")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-no-match", name="Zeta Ltd", domain="zeta.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "needle", "sort": "name", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    ids = {item["account_id"] for item in response.json()["items"]}
    assert ids == {"acct-name-match", "acct-domain-match"}


async def test_list_accounts_q_too_short_returns_422_invalid_account_search(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "a"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SEARCH"


async def test_list_accounts_q_too_long_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "a" * 201},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SEARCH"


async def test_list_accounts_q_empty_string_treated_as_omitted_returns_200(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": ""}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_list_accounts_q_whitespace_only_treated_as_omitted(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "   "}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_list_accounts_q_percent_wildcard_is_literal_not_match_all(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-percent", name="100% Corp", domain="pct.example")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-other", name="Zeta Ltd", domain="zeta.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "0%"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    ids = [item["account_id"] for item in response.json()["items"]]
    assert ids == ["acct-percent"]


async def test_list_accounts_q_underscore_wildcard_is_literal(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-underscore", name="A_B Corp", domain="ab.example")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-other", name="AxB Corp", domain="axb.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "A_B"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    ids = [item["account_id"] for item in response.json()["items"]]
    assert ids == ["acct-underscore"]


async def test_list_accounts_q_changes_total_count(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-match", name="Needle Corp")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-no-match", name="Zeta Ltd", domain="zeta.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", params={"q": "needle"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_list_accounts_sort_does_not_change_total_count(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 2


@pytest.mark.parametrize("sort", ["name", "domain", "created"])
async def test_list_accounts_sort_cross_tenant_isolation_every_sort_key(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    # Tenant B's row is seeded to sort FIRST for every key under test, so a
    # test that could leak would actually leak, per the plan's seeding rule.
    db.seed(tenant_id=_OTHER_TENANT_ID, account_id="acct-other", name="AAA Corp", domain="aaa.example")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-mine", name="Zzz Corp", domain="zzz.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 200
    ids = [item["account_id"] for item in response.json()["items"]]
    assert ids == ["acct-mine"]


async def test_list_accounts_q_cross_tenant_isolation(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_OTHER_TENANT_ID, account_id="acct-other", name="Needle Corp")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-mine", name="Zeta Ltd", domain="zeta.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"q": "needle"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_list_accounts_sort_cross_tenant_isolation_with_injection_payload(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_OTHER_TENANT_ID, account_id="acct-other")
    db.seed(tenant_id=_TENANT_ID, account_id="acct-mine")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "tenant_id"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_client_agent_may_sort_and_search(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name", "dir": "asc", "q": "acme"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_accounts_visitor_still_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name"},
            cookies={"access_token": _token(Role.VISITOR)},
        )

    assert response.status_code == 403


async def test_list_accounts_no_auth_still_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/accounts", params={"sort": "name"})

    assert response.status_code == 401


async def test_list_accounts_platform_admin_implicit_route_still_403(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"sort": "name"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 403


async def test_list_accounts_tenant_scoped_route_accepts_sort_and_q(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1", name="Acme Ltd")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts",
            params={"sort": "name", "dir": "asc", "q": "acme"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 200
    assert [item["account_id"] for item in response.json()["items"]] == ["acct-1"]


async def test_list_accounts_tenant_scoped_route_sort_unknown_key_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts",
            params={"sort": "bogus"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SORT"


async def test_list_accounts_tenant_scoped_route_q_too_short_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/accounts",
            params={"q": "a"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT_SEARCH"


async def test_list_accounts_existing_pagination_behavior_unchanged(
    app: Any, db: _StubDatabase,
) -> None:
    for i in range(3):
        db.seed(tenant_id=_TENANT_ID, account_id=f"acct-{i}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts",
            params={"limit": 2, "offset": 0},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2


async def test_list_accounts_response_shape_has_no_new_fields(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/accounts", cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert set(item.keys()) == {"account_id", "name", "domain", "created_at"}
