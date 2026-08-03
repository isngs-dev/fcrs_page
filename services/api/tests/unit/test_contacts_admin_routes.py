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

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q:
            tenant_id, account_id = args[0], args[1]
            return self._accounts.get((tenant_id, account_id))
        if "COUNT(*)" in q and "FROM CONTACTS" in q:
            tenant_id = args[0]
            rows = [r for r in self._contacts.values() if r["tenant_id"] == tenant_id]
            return {"count": len(rows)}
        if "FROM CONTACTS" in q and "WHERE TENANT_ID" in q and "CONTACT_ID = $2" in q:
            tenant_id, contact_id = args[0], args[1]
            return self._contacts.get((tenant_id, contact_id))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM CONTACTS" in q:
            tenant_id = args[0]
            rows = [r for r in self._contacts.values() if r["tenant_id"] == tenant_id]
            rows.sort(key=lambda r: (r["created_at"], r["contact_id"]), reverse=True)
            if "LIMIT $" in q:
                limit = args[1]
                offset = args[2] if len(args) > 2 else 0
                rows = rows[offset : offset + limit]
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
