"""Unit tests for /admin/contacts (POST/GET list/GET detail/PATCH) +
/admin/tenants/{tenant_id}/contacts tenant-explicit twins.

Covers (SR-9.2 D7/D8/D9):
- POST with valid consent -> 201; no consent / granted=false -> 422
  CONSENT_REQUIRED, nothing persisted.
- POST with an account_id belonging to a DIFFERENT tenant -> 422
  INVALID_ACCOUNT.
- RBAC per D8: CLIENT_ADMIN full; CLIENT_AGENT read-only; VISITOR 403
  everywhere; PLATFORM_ADMIN 403 implicit / 200 tenant-explicit + 404
  TENANT_NOT_FOUND for unknown tenant, honest audit actor.
- Cross-tenant GET -> 404.
- PATCH: only supplied fields change; account_id: null unlinks; PATCH on
  missing/cross-tenant contact -> 404.
- Response never contains tenant_id or raw consent text.
- PII-safe logging (caplog).

SR-29 additions: real ``?sort=``/``?dir=``/``?q=``/``?account_id=`` on
``GET /admin/contacts`` and its tenant-scoped twin. Ordering *correctness* is
proven only against real Postgres (``test_crm_list_sort_integration.py``) --
these tests own contract/validation/RBAC and *which rows* come back, D-TEST.
"""
from __future__ import annotations

import logging
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

_PII_NAME = "Dana Contact"
_PII_EMAIL = "dana.contact@example.com"
_PII_PHONE = "+15559876543"
_PII_CONSENT_TEXT = "I consent to being stored as a CRM contact record."


class _StubDatabase:
    """In-memory stub database backing /admin/contacts for these tests."""

    def __init__(self) -> None:
        self._contacts: dict[tuple[str, str], dict[str, Any]] = {}
        self._accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self._tenants: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    def seed_tenant(self, *, tenant_id: str, slug: str, enabled: bool = True) -> None:
        self._tenants[tenant_id] = {
            "id": tenant_id, "name": slug, "slug": slug, "enabled": enabled,
        }

    def seed_account(self, *, tenant_id: str, account_id: str, name: str = "Acme Ltd") -> None:
        self._accounts[(tenant_id, account_id)] = {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "name": name,
            "domain": None,
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    def seed_contact(
        self,
        *,
        tenant_id: str,
        contact_id: str,
        account_id: str | None = None,
        name: str = _PII_NAME,
        email: str | None = _PII_EMAIL,
        phone: str | None = _PII_PHONE,
    ) -> None:
        self._contacts[(tenant_id, contact_id)] = {
            "tenant_id": tenant_id,
            "contact_id": contact_id,
            "account_id": account_id,
            "lead_id": None,
            "name": name,
            "email": email,
            "phone": phone,
            "consent": {"granted": True, "purpose": "crm", "text": _PII_CONSENT_TEXT, "captured_at": "2026-01-01T12:00:00Z"},
            "owner_agent_id": None,
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    def _filtered_contacts(
        self, query: str, args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        """Apply WHERE/ORDER BY/LIMIT the way the real repository emits them.

        Hard-fails (``AssertionError``) on any ORDER BY or WHERE fragment it
        does not recognize, rather than silently ignoring it -- SR-29 F3/8a:
        a stub that guesses converts a loud failure into a quiet wrong answer.
        """
        q = query.strip().upper()
        tenant_id = args[0]
        rows = [r for r in self._contacts.values() if r["tenant_id"] == tenant_id]
        idx = 1

        if "ACCOUNT_ID = $" in q:
            account_id = args[idx]
            idx += 1
            rows = [row for row in rows if row["account_id"] == account_id]

        if "NAME ILIKE $" in q and "EMAIL ILIKE $" in q:
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
                or needle in (row["email"] or "").lower()
            ]

        if "ORDER BY " not in q:
            return rows, len(rows)

        if "ORDER BY NAME " in q:
            value_for = lambda row: row["name"]  # noqa: E731
        elif "ORDER BY EMAIL " in q:
            value_for = lambda row: row["email"]  # noqa: E731
        elif "ORDER BY ACCOUNT_ID " in q:
            value_for = lambda row: row["account_id"]  # noqa: E731
        elif "ORDER BY OWNER_AGENT_ID " in q:
            value_for = lambda row: row["owner_agent_id"]  # noqa: E731
        elif "ORDER BY CREATED_AT " in q:
            value_for = lambda row: row["created_at"]  # noqa: E731
        else:
            raise AssertionError(f"stub cannot honor ORDER BY: {query}")

        descending = " DESC NULLS LAST" in q
        non_null_rows = [row for row in rows if value_for(row) is not None]
        null_rows = [row for row in rows if value_for(row) is None]
        non_null_rows.sort(key=lambda row: row["contact_id"], reverse=True)
        non_null_rows.sort(key=value_for, reverse=descending)
        null_rows.sort(key=lambda row: row["contact_id"], reverse=True)
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
        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q:
            tenant_id, account_id = args[0], args[1]
            return self._accounts.get((tenant_id, account_id))
        if "COUNT(*)" in q and "FROM CONTACTS" in q:
            rows, total = self._filtered_contacts(query, args)
            return {"count": total}
        if "FROM CONTACTS" in q and "WHERE TENANT_ID" in q and "CONTACT_ID = $2" in q:
            tenant_id, contact_id = args[0], args[1]
            return self._contacts.get((tenant_id, contact_id))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM CONTACTS" in q:
            rows, _ = self._filtered_contacts(query, args)
            return rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO CONTACTS"):
            (
                tenant_id, contact_id, account_id, lead_id, name, email,
                phone, consent, owner_agent_id,
            ) = args
            self._contacts[(tenant_id, contact_id)] = {
                "tenant_id": tenant_id,
                "contact_id": contact_id,
                "account_id": account_id,
                "lead_id": lead_id,
                "name": name,
                "email": email,
                "phone": phone,
                "consent": consent,
                "owner_agent_id": owner_agent_id,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE CONTACTS"):
            tenant_id = args[-2]
            contact_id = args[-1]
            existing = self._contacts.get((tenant_id, contact_id))
            if existing is None:
                return "UPDATE 0"
            set_part = query.split("SET", 1)[1].split("WHERE", 1)[0]
            columns = [c.strip().split("=")[0].strip() for c in set_part.split(",")]
            for col, val in zip(columns, args[:-2], strict=False):
                if col == "updated_at":
                    continue
                existing[col] = val
            existing["updated_at"] = _NOW
            return "UPDATE 1"
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


def _valid_consent() -> dict[str, Any]:
    return {"granted": True, "purpose": "crm", "text": _PII_CONSENT_TEXT}


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
# POST /admin/contacts
# ---------------------------------------------------------------------------


async def test_post_with_valid_consent_returns_201(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/contacts",
            json={"name": _PII_NAME, "email": _PII_EMAIL, "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == _PII_NAME
    assert "tenant_id" not in data
    assert "consent" not in data


async def test_post_no_consent_returns_422_and_nothing_persisted(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/contacts",
            json={"name": _PII_NAME, "email": _PII_EMAIL},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "CONSENT_REQUIRED"
    assert db._contacts == {}


async def test_post_consent_granted_false_returns_422_and_nothing_persisted(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/contacts",
            json={
                "name": _PII_NAME,
                "consent": {"granted": False, "purpose": "crm", "text": _PII_CONSENT_TEXT},
            },
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "CONSENT_REQUIRED"
    assert db._contacts == {}


async def test_post_account_id_from_different_tenant_returns_422(app: Any, db: _StubDatabase) -> None:
    db.seed_account(tenant_id=_OTHER_TENANT_ID, account_id="acct-foreign")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/contacts",
            json={"name": _PII_NAME, "account_id": "acct-foreign", "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT"
    assert db._contacts == {}


async def test_post_valid_account_id_same_tenant_succeeds(app: Any, db: _StubDatabase) -> None:
    db.seed_account(tenant_id=_TENANT_ID, account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/contacts",
            json={"name": _PII_NAME, "account_id": "acct-1", "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    assert response.json()["account_id"] == "acct-1"


async def test_client_agent_post_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/contacts",
            json={"name": _PII_NAME, "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_visitor_post_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post(
            "/admin/contacts",
            json={"name": _PII_NAME, "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_no_auth_post_returns_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/contacts", json={"name": _PII_NAME, "consent": _valid_consent()},
        )

    assert response.status_code == 401


async def test_platform_admin_post_implicit_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.post(
            "/admin/contacts", json={"name": _PII_NAME, "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_platform_admin_post_tenant_explicit_returns_201_with_honest_audit(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="pa-real-id")
        response = await client.post(
            f"/admin/tenants/{_TENANT_ID}/contacts",
            json={"name": _PII_NAME, "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    rows = [r for r in db.audit_rows if r["action"] == "contact_created"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-real-id"
    assert rows[0]["metadata"]["platform_admin"] is True


async def test_platform_admin_post_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.post(
            "/admin/tenants/does-not-exist/contacts",
            json={"name": _PII_NAME, "consent": _valid_consent()},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /admin/contacts (list)
# ---------------------------------------------------------------------------


async def test_client_admin_list_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/contacts", cookies={"access_token": token})

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert "tenant_id" not in data["items"][0]
    assert "consent" not in data["items"][0]


async def test_client_agent_list_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/contacts", cookies={"access_token": token})

    assert response.status_code == 200


async def test_visitor_list_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/contacts", cookies={"access_token": token})

    assert response.status_code == 403


async def test_no_auth_list_returns_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/contacts")

    assert response.status_code == 401


async def test_platform_admin_list_tenant_explicit_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/contacts", cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_platform_admin_list_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/tenants/does-not-exist/contacts", cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


# ---------------------------------------------------------------------------
# SR-29: list sort + combined name/email search + account_id filter contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "created_at; DROP TABLE contacts--",
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
async def test_list_contacts_sort_sql_injection_payload_returns_422(
    app: Any, db: _StubDatabase, payload: str,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": payload},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT"


async def test_list_contacts_sort_tenant_id_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "tenant_id"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT"


async def test_list_contacts_dir_unknown_returns_422_invalid_contact_sort_direction(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "name", "dir": "sideways"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT_DIRECTION"


@pytest.mark.parametrize("sort", ["name", "email", "account", "owner", "created"])
async def test_list_contacts_all_five_sort_keys_return_200(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id=f"contact-{sort}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_contacts_sort_by_owner_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "owner", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


async def test_list_contacts_sort_by_account_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "account", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


async def test_list_contacts_q_matches_name_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="Alice Needle", email="a@x.example")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", name="Bob Other", email="b@x.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "needle"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert [i["contact_id"] for i in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_q_matches_email_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="Alice", email="needle@x.example")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", name="Bob", email="other@x.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "NEEDLE"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert [i["contact_id"] for i in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_q_too_short_returns_422_invalid_contact_search(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "a"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SEARCH"


async def test_list_contacts_q_too_long_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"q": "a" * 201},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SEARCH"


async def test_list_contacts_q_empty_string_treated_as_omitted_returns_200(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": ""}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


async def test_list_contacts_q_percent_wildcard_is_literal(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="100% Match")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", name="No Percent")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "100%"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert [i["contact_id"] for i in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_account_id_filter_narrows_to_that_account(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_account(tenant_id=_TENANT_ID, account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-1"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert [i["contact_id"] for i in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_account_id_filter_unknown_id_returns_200_empty(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "does-not-exist"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_list_contacts_account_id_filter_from_other_tenant_returns_empty(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-other", account_id="acct-other")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-other"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_list_contacts_account_id_filter_composes_with_q_and_sort(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(
        tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1", name="Needle A",
    )
    db.seed_contact(
        tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-1", name="Other B",
    )
    db.seed_contact(
        tenant_id=_TENANT_ID, contact_id="contact-3", account_id="acct-2", name="Needle C",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-1", "q": "needle", "sort": "name", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert [i["contact_id"] for i in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_account_id_filter_changes_total_count(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-1"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.json()["total"] == 1


@pytest.mark.parametrize("sort", ["name", "email", "account", "owner", "created"])
async def test_list_contacts_cross_tenant_isolation_every_sort_key(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-mine", name="Zzz Mine", email="zzz@x.example")
    db.seed_contact(
        tenant_id=_OTHER_TENANT_ID, contact_id="contact-other", name="AAA Other", email="aaa@x.example",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    ids = [i["contact_id"] for i in response.json()["items"]]
    assert ids == ["contact-mine"]


async def test_list_contacts_q_cross_tenant_isolation(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-mine", name="Needle Corp")
    db.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-other", name="Needle Corp")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"q": "needle"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    ids = [i["contact_id"] for i in response.json()["items"]]
    assert ids == ["contact-mine"]


async def test_list_contacts_client_agent_may_sort_and_search(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="Alice")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "name", "dir": "asc", "q": "alice"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_contacts_visitor_still_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "name"},
            cookies={"access_token": _token(Role.VISITOR)},
        )

    assert response.status_code == 403


async def test_list_contacts_no_auth_still_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/contacts", params={"sort": "name"})

    assert response.status_code == 401


async def test_list_contacts_platform_admin_implicit_route_still_403(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/contacts", params={"sort": "name"}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_list_contacts_tenant_scoped_route_accepts_sort_and_q(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="Acme")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/contacts",
            params={"sort": "name", "dir": "asc", "q": "acme"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert [i["contact_id"] for i in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_tenant_scoped_route_accepts_account_id_filter(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/contacts",
            params={"account_id": "acct-1"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert [i["contact_id"] for i in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_response_shape_has_no_new_fields(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/contacts", cookies={"access_token": _token(Role.CLIENT_ADMIN)})

    item = response.json()["items"][0]
    assert set(item.keys()) == {
        "contact_id", "account_id", "lead_id", "name", "email", "phone",
        "owner_agent_id", "created_at",
    }


# ---------------------------------------------------------------------------
# GET /admin/contacts/{contact_id}
# ---------------------------------------------------------------------------


async def test_client_admin_get_detail_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/contacts/contact-1", cookies={"access_token": token})

    assert response.status_code == 200
    data = response.json()
    assert data["contact_id"] == "contact-1"
    assert "tenant_id" not in data
    assert "consent" not in data


async def test_client_agent_get_detail_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/contacts/contact-1", cookies={"access_token": token})

    assert response.status_code == 200


async def test_get_detail_unknown_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/contacts/does-not-exist", cookies={"access_token": token})

    assert response.status_code == 404


async def test_cross_tenant_get_detail_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, tenant_id=_OTHER_TENANT_ID)
        response = await client.get("/admin/contacts/contact-1", cookies={"access_token": token})

    assert response.status_code == 404


async def test_visitor_get_detail_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/contacts/contact-1", cookies={"access_token": token})

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /admin/contacts/{contact_id}
# ---------------------------------------------------------------------------


async def test_patch_only_supplied_fields_change(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/contacts/contact-1",
            json={"name": "Updated Name"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Name"
    assert data["email"] == _PII_EMAIL
    assert data["phone"] == _PII_PHONE


async def test_patch_account_id_null_unlinks(app: Any, db: _StubDatabase) -> None:
    db.seed_account(tenant_id=_TENANT_ID, account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/contacts/contact-1",
            json={"account_id": None},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    assert response.json()["account_id"] is None


async def test_patch_account_id_different_tenant_returns_422(app: Any, db: _StubDatabase) -> None:
    db.seed_account(tenant_id=_OTHER_TENANT_ID, account_id="acct-foreign")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/contacts/contact-1",
            json={"account_id": "acct-foreign"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT"


async def test_patch_missing_contact_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/contacts/does-not-exist",
            json={"name": "X"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404


async def test_patch_cross_tenant_contact_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        response = await client.patch(
            "/admin/contacts/contact-1",
            json={"name": "Hacked"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    stored = db._contacts[(_TENANT_ID, "contact-1")]
    assert stored["name"] == _PII_NAME


async def test_client_agent_patch_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.patch(
            "/admin/contacts/contact-1",
            json={"name": "Hacked"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_visitor_patch_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.patch(
            "/admin/contacts/contact-1",
            json={"name": "Hacked"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_platform_admin_patch_implicit_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.patch(
            "/admin/contacts/contact-1",
            json={"name": "X"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_platform_admin_patch_tenant_explicit_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="pa-1")
        response = await client.patch(
            f"/admin/tenants/{_TENANT_ID}/contacts/contact-1",
            json={"name": "Updated"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    rows = [r for r in db.audit_rows if r["action"] == "contact_updated"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-1"
    assert rows[0]["metadata"]["platform_admin"] is True


async def test_platform_admin_patch_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.patch(
            "/admin/tenants/does-not-exist/contacts/contact-1",
            json={"name": "X"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


# ---------------------------------------------------------------------------
# PII discipline
# ---------------------------------------------------------------------------


async def test_pii_not_logged_on_post(app: Any, db: _StubDatabase, caplog: Any) -> None:
    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.post(
                "/admin/contacts",
                json={
                    "name": _PII_NAME,
                    "email": _PII_EMAIL,
                    "phone": _PII_PHONE,
                    "consent": _valid_consent(),
                },
                cookies={"access_token": token},
            )

    log_text = caplog.text
    assert _PII_NAME not in log_text
    assert _PII_EMAIL not in log_text
    assert _PII_PHONE not in log_text
    assert _PII_CONSENT_TEXT not in log_text


async def test_pii_not_logged_on_patch(app: Any, db: _StubDatabase, caplog: Any) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.patch(
                "/admin/contacts/contact-1",
                json={"name": "New Name", "email": "new@example.com", "phone": "+15550000000"},
                cookies={"access_token": token},
            )

    log_text = caplog.text
    assert _PII_NAME not in log_text
    assert _PII_EMAIL not in log_text
    assert _PII_PHONE not in log_text
    assert "New Name" not in log_text
    assert "new@example.com" not in log_text


async def test_pii_not_logged_on_list(app: Any, db: _StubDatabase, caplog: Any) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.get("/admin/contacts", cookies={"access_token": token})

    log_text = caplog.text
    assert _PII_NAME not in log_text
    assert _PII_EMAIL not in log_text
    assert _PII_PHONE not in log_text


# ---------------------------------------------------------------------------
# SR-29: GET /admin/contacts -- sort / search / account_id filter
# ---------------------------------------------------------------------------


async def test_list_contacts_sort_omitted_preserves_created_desc_default(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        "created_at; DROP TABLE contacts--",
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
async def test_list_contacts_sort_unknown_key_returns_422_invalid_contact_sort(
    app: Any, db: _StubDatabase, payload: str,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": payload},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT"


async def test_list_contacts_sort_sql_injection_payload_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "created_at; DROP TABLE contacts--"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT"


async def test_list_contacts_dir_unknown_returns_422_invalid_contact_sort_direction(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "name", "dir": "sideways"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT_DIRECTION"


@pytest.mark.parametrize("sort", ["name", "email", "account", "owner", "created"])
async def test_list_contacts_all_five_sort_keys_return_200(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id=f"contact-sort-{sort}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_contacts_sort_by_owner_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "owner", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


async def test_list_contacts_sort_by_account_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "account", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200


async def test_list_contacts_q_matches_name_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="Alice Needle", email="alice@other.example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "NEEDLE"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [item["contact_id"] for item in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_q_matches_email_substring_case_insensitive(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="Bob Smith", email="needle@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "NEEDLE"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [item["contact_id"] for item in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_q_matches_either_field_ored(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-name-match", name="Needle Person", email="other@example.com")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-email-match", name="Zeta Person", email="needle@example.com")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-no-match", name="Gamma Person", email="gamma@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"q": "needle", "sort": "name", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    ids = {item["contact_id"] for item in response.json()["items"]}
    assert ids == {"contact-name-match", "contact-email-match"}


async def test_list_contacts_q_too_short_returns_422_invalid_contact_search(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "a"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SEARCH"


async def test_list_contacts_q_too_long_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"q": "a" * 201},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SEARCH"


async def test_list_contacts_q_empty_string_treated_as_omitted_returns_200(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": ""}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_list_contacts_q_whitespace_only_treated_as_omitted(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "   "}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_list_contacts_q_percent_wildcard_is_literal_not_match_all(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-percent", name="100% Match", email="pct@example.com")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-other", name="Zeta Person", email="zeta@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "0%"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    ids = [item["contact_id"] for item in response.json()["items"]]
    assert ids == ["contact-percent"]


async def test_list_contacts_q_underscore_wildcard_is_literal(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-underscore", name="A_B Person", email="ab@example.com")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-other", name="AxB Person", email="axb@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "A_B"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    ids = [item["contact_id"] for item in response.json()["items"]]
    assert ids == ["contact-underscore"]


async def test_list_contacts_q_changes_total_count(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-match", name="Needle Person")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-no-match", name="Zeta Person", email="zeta@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", params={"q": "needle"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_list_contacts_sort_does_not_change_total_count(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "name", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 2


@pytest.mark.parametrize("sort", ["name", "email", "account", "owner", "created"])
async def test_list_contacts_sort_cross_tenant_isolation_every_sort_key(
    app: Any, db: _StubDatabase, sort: str,
) -> None:
    # Tenant B's row is seeded to sort FIRST for every key under test, so a
    # test that could leak would actually leak, per the plan's seeding rule.
    db.seed_contact(
        tenant_id=_OTHER_TENANT_ID, contact_id="contact-other",
        name="AAA Person", email="aaa@example.com", account_id="aaa-account",
    )
    db._contacts[(_OTHER_TENANT_ID, "contact-other")]["owner_agent_id"] = "aaa-owner"
    db.seed_contact(
        tenant_id=_TENANT_ID, contact_id="contact-mine",
        name="Zzz Person", email="zzz@example.com", account_id="zzz-account",
    )
    db._contacts[(_TENANT_ID, "contact-mine")]["owner_agent_id"] = "zzz-owner"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 200
    ids = [item["contact_id"] for item in response.json()["items"]]
    assert ids == ["contact-mine"]


async def test_list_contacts_q_cross_tenant_isolation(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-other", name="Needle Person")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-mine", name="Zeta Person", email="zeta@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"q": "needle"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_list_contacts_sort_cross_tenant_isolation_with_injection_payload(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-other")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-mine")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "tenant_id"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT"


async def test_list_contacts_client_agent_may_sort_and_search(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "name", "dir": "asc", "q": "alice"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_contacts_visitor_still_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"sort": "name"},
            cookies={"access_token": _token(Role.VISITOR)},
        )

    assert response.status_code == 403


async def test_list_contacts_no_auth_still_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/contacts", params={"sort": "name"})

    assert response.status_code == 401


async def test_list_contacts_tenant_scoped_route_accepts_sort_and_q(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", name="Alice Needle")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/contacts",
            params={"sort": "name", "dir": "asc", "q": "needle"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 200
    assert [item["contact_id"] for item in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_tenant_scoped_route_sort_unknown_key_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/contacts",
            params={"sort": "bogus"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SORT"


async def test_list_contacts_tenant_scoped_route_q_too_short_returns_422(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/contacts",
            params={"q": "a"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT_SEARCH"


async def test_list_contacts_tenant_scoped_route_accepts_account_id_filter(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/contacts",
            params={"account_id": "acct-1"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 200
    assert [item["contact_id"] for item in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_existing_pagination_behavior_unchanged(
    app: Any, db: _StubDatabase,
) -> None:
    for i in range(3):
        db.seed_contact(tenant_id=_TENANT_ID, contact_id=f"contact-{i}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"limit": 2, "offset": 0},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2


async def test_list_contacts_response_shape_has_no_new_fields(app: Any, db: _StubDatabase) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts", cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert set(item.keys()) == {
        "contact_id", "account_id", "lead_id", "name", "email", "phone",
        "owner_agent_id", "created_at",
    }


# ---------------------------------------------------------------------------
# SR-29: ?account_id= filter (D-FILTER)
# ---------------------------------------------------------------------------


async def test_list_contacts_account_id_filter_narrows_to_that_account(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-1"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [item["contact_id"] for item in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_account_id_filter_unknown_id_returns_200_empty(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "does-not-exist"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_list_contacts_account_id_filter_from_other_tenant_returns_empty(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-other", account_id="acct-foreign")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-foreign"},
            cookies={"access_token": _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)},
        )

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_list_contacts_account_id_filter_composes_with_q_and_sort(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1", name="Needle One")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-2", name="Needle Two")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-3", account_id="acct-1", name="Gamma Three")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-1", "q": "needle", "sort": "name", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert [item["contact_id"] for item in response.json()["items"]] == ["contact-1"]


async def test_list_contacts_account_id_filter_changes_total_count(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1", account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/contacts",
            params={"account_id": "acct-1"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
