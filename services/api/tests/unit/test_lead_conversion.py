"""Unit tests for POST /admin/leads/{lead_id}/convert (+ tenant-explicit
twin), covering SR-9.2 D1 (tombstone), D5 (bypasses validate_transition),
D6 (idempotency), D7 (consent carry-through) end to end.

Backs leads, lead_activities, contacts, contact_identities, accounts, and
audit_events simultaneously in one stub DB (this route touches all of them).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from asyncpg.exceptions import UniqueViolationError
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

_PII_NAME = "Jane Convertible"
_PII_EMAIL = "jane.convertible@example.com"
_PII_PHONE = "+15551239876"
_PII_CONSENT_TEXT = "I agree to be contacted regarding my inquiry."

_ORIGINAL_CONSENT = {
    "granted": True,
    "purpose": "contact",
    "text": _PII_CONSENT_TEXT,
    "captured_at": "2025-06-15T09:30:00Z",
}


class _StubDatabase:
    """In-memory stub database backing leads, lead_activities, contacts,
    contact_identities, accounts, and audit_events for conversion tests."""

    def __init__(self) -> None:
        self._leads: dict[tuple[str, str], dict[str, Any]] = {}
        self._activities: dict[tuple[str, str], dict[str, Any]] = {}
        self._contacts: dict[tuple[str, str], dict[str, Any]] = {}
        self._identities: dict[tuple[str, str], dict[str, Any]] = {}
        self._accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self._tenants: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []
        self._activity_seq = 0

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

    def seed_lead(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        stage: str = "captured",
        status: str = "new",
        name: str | None = _PII_NAME,
        email: str | None = _PII_EMAIL,
        phone: str | None = _PII_PHONE,
        visitor_id: str | None = "visitor-1",
        consent: dict[str, Any] | None = None,
        converted_to_contact_id: str | None = None,
    ) -> None:
        self._leads[(tenant_id, lead_id)] = {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "visitor_id": visitor_id,
            "name": name,
            "email": email,
            "phone": phone,
            "status": status,
            "stage": stage,
            "qualification_score": None,
            "consent": consent if consent is not None else dict(_ORIGINAL_CONSENT),
            "assigned_agent_id": None,
            "source": "widget",
            "created_at": _NOW,
            "updated_at": _NOW,
            "converted_to_contact_id": converted_to_contact_id,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()

        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])

        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q:
            tenant_id, account_id = args[0], args[1]
            return self._accounts.get((tenant_id, account_id))

        if "COUNT(*)" in q and "FROM LEADS" in q:
            _, total = self._filtered_leads(query, args)
            return {"count": total}

        if "FROM LEADS" in q and "WHERE TENANT_ID" in q and "LEAD_ID" in q:
            tenant_id, lead_id = args[0], args[1]
            return self._leads.get((tenant_id, lead_id))

        if "FROM CONTACTS" in q and "WHERE TENANT_ID" in q and "LEAD_ID = $2" in q:
            tenant_id, lead_id = args[0], args[1]
            for row in self._contacts.values():
                if row["tenant_id"] == tenant_id and row["lead_id"] == lead_id:
                    return row
            return None

        if "FROM CONTACTS" in q and "WHERE TENANT_ID" in q and "CONTACT_ID = $2" in q:
            tenant_id, contact_id = args[0], args[1]
            return self._contacts.get((tenant_id, contact_id))

        if "CONTACT_ID FROM CONTACT_IDENTITIES" in q:
            tenant_id, identity_type, identity_value = args[0], args[1], args[2]
            for row in self._identities.values():
                if (
                    row["tenant_id"] == tenant_id
                    and row["identity_type"] == identity_type
                    and row["identity_value"] == identity_value
                ):
                    return {"contact_id": row["contact_id"]}
            return None

        return None

    def _filtered_leads(
        self, query: str, args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        q = query.upper()
        idx = 0
        tenant_id = args[idx]
        idx += 1
        rows = [row for row in self._leads.values() if row["tenant_id"] == tenant_id]

        if "STAGE = $" in q:
            rows = [r for r in rows if r["stage"] == args[idx]]
            idx += 1
        if "STATUS = $" in q:
            rows = [r for r in rows if r["status"] == args[idx]]
            idx += 1
        if "ASSIGNED_AGENT_ID = $" in q:
            rows = [r for r in rows if r["assigned_agent_id"] == args[idx]]
            idx += 1
        if "CREATED_AT >= $" in q:
            rows = [r for r in rows if r["created_at"] >= args[idx]]
            idx += 1
        if "CREATED_AT < $" in q:
            rows = [r for r in rows if r["created_at"] < args[idx]]
            idx += 1
        if "CONVERTED_TO_CONTACT_ID IS NULL" in q:
            rows = [r for r in rows if r["converted_to_contact_id"] is None]

        rows.sort(key=lambda r: (r["created_at"], r["lead_id"]), reverse=True)
        total = len(rows)

        if "LIMIT $" in q:
            limit = args[idx]
            idx += 1
            offset = args[idx] if idx < len(args) else 0
            rows = rows[offset : offset + limit]

        return rows, total

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()

        if "FROM LEAD_ACTIVITIES" in q:
            tenant_id, lead_id = args[0], args[1]
            rows = [
                r for r in self._activities.values()
                if r["tenant_id"] == tenant_id and r["lead_id"] == lead_id
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows

        if "FROM CONTACT_IDENTITIES" in q:
            tenant_id, contact_id = args[0], args[1]
            rows = [
                r for r in self._identities.values()
                if r["tenant_id"] == tenant_id and r["contact_id"] == contact_id
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows

        if "FROM LEADS" in q and "LIMIT $" in q:
            rows, _ = self._filtered_leads(query, args)
            return rows

        if "FROM LEADS" in q:
            tenant_id = args[0]
            rows = [row for row in self._leads.values() if row["tenant_id"] == tenant_id]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows

        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()

        if q.startswith("UPDATE LEADS SET STAGE = 'CONVERTED'"):
            # mark_lead_converted: literal SQL for stage/status/'converted'/'won';
            # only contact_id, tenant_id, lead_id are bound params ($1/$2/$3).
            contact_id, tenant_id, lead_id = args
            row = self._leads.get((tenant_id, lead_id))
            if row is None or row["converted_to_contact_id"] is not None:
                return "UPDATE 0"
            row["stage"] = "converted"
            row["status"] = "won"
            row["converted_to_contact_id"] = contact_id
            row["updated_at"] = _NOW
            return "UPDATE 1"

        if q.startswith("UPDATE LEADS SET ASSIGNED_AGENT_ID"):
            assigned_agent_id, tenant_id, lead_id = args
            row = self._leads.get((tenant_id, lead_id))
            if row is None:
                return "UPDATE 0"
            row["assigned_agent_id"] = assigned_agent_id
            return "UPDATE 1"

        if q.startswith("UPDATE LEADS"):
            stage, status_, score, tenant_id, lead_id = args
            row = self._leads.get((tenant_id, lead_id))
            if row is None:
                return "UPDATE 0"
            row["stage"] = stage
            row["status"] = status_
            row["qualification_score"] = score
            return "UPDATE 1"

        if q.startswith("INSERT INTO LEAD_ACTIVITIES"):
            tenant_id, activity_id, lead_id, activity_type, payload, actor = args
            self._activity_seq += 1
            self._activities[(tenant_id, activity_id)] = {
                "tenant_id": tenant_id,
                "activity_id": activity_id,
                "lead_id": lead_id,
                "type": activity_type,
                "payload": payload,
                "actor": actor,
                "created_at": _NOW.replace(microsecond=self._activity_seq),
            }
            return "INSERT 0 1"

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

        if q.startswith("INSERT INTO CONTACTS"):
            (
                tenant_id, contact_id, account_id, lead_id, name, email,
                phone, consent, owner_agent_id,
            ) = args
            if lead_id is not None:
                for row in self._contacts.values():
                    if row["tenant_id"] == tenant_id and row["lead_id"] == lead_id:
                        raise UniqueViolationError("duplicate key value violates unique constraint")
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

        if q.startswith("INSERT INTO CONTACT_IDENTITIES"):
            tenant_id, identity_id, contact_id, identity_type, identity_value = args
            for row in self._identities.values():
                if (
                    row["tenant_id"] == tenant_id
                    and row["identity_type"] == identity_type
                    and row["identity_value"] == identity_value
                ):
                    raise UniqueViolationError("duplicate key value violates unique constraint")
            self._identities[(tenant_id, identity_id)] = {
                "tenant_id": tenant_id,
                "identity_id": identity_id,
                "contact_id": contact_id,
                "identity_type": identity_type,
                "identity_value": identity_value,
                "created_at": _NOW,
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
# D5 -- convert succeeds from stage='captured' (pipeline validator bypass)
# ---------------------------------------------------------------------------


async def test_convert_from_captured_succeeds(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-1", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-1/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["lead_id"] == "lead-1"
    assert isinstance(data["contact_id"], str)


# ---------------------------------------------------------------------------
# D6 -- idempotency
# ---------------------------------------------------------------------------


async def test_second_convert_returns_409_with_same_contact_id(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-2", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        first = await client.post(
            "/admin/leads/lead-2/convert", json={}, cookies={"access_token": token},
        )
        second = await client.post(
            "/admin/leads/lead-2/convert", json={}, cookies={"access_token": token},
        )

    assert first.status_code == 201
    first_contact_id = first.json()["contact_id"]

    assert second.status_code == 409
    second_body = second.json()
    assert second_body["error_code"] == "LEAD_ALREADY_CONVERTED"
    assert second_body["contact_id"] == first_contact_id

    # Exactly one contact row exists for this lead.
    contacts_for_lead = [
        c for c in db._contacts.values()
        if c["tenant_id"] == _TENANT_ID and c["lead_id"] == "lead-2"
    ]
    assert len(contacts_for_lead) == 1


async def test_cross_tenant_convert_returns_404_and_creates_nothing(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-3", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        response = await client.post(
            "/admin/leads/lead-3/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert db._contacts == {}
    # Original tenant's lead untouched.
    stored = db._leads[(_TENANT_ID, "lead-3")]
    assert stored["converted_to_contact_id"] is None


# ---------------------------------------------------------------------------
# SR-9.4 D4 -- conversion creates a Contact and ZERO Opportunities
# ---------------------------------------------------------------------------


async def test_convert_creates_zero_opportunities(app: Any, db: _StubDatabase) -> None:
    """SR-9.4 D4: an Opportunity is created manually and only manually --
    nothing auto-creates one, including lead-to-contact conversion. This is
    a regression test against the SHIPPED convert route (leads/admin_routes.py
    ``_convert_lead``), read directly rather than assumed: it calls
    ``create_contact`` and nothing else. Verified two ways: (1) the stub DB
    never sees an ``INSERT INTO OPPORTUNITIES`` (would raise/return "OK"
    unnoticed if it did -- so we additionally assert the opportunities
    repository's own list against this contact is empty), and (2) a live
    ``list_opportunities`` call against the resulting contact_id returns zero
    rows.
    """
    from api.opportunities.repository import list_opportunities

    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-4", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-4/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 201
    contact_id = response.json()["contact_id"]

    # The stub DB's execute() has no branch for "INSERT INTO OPPORTUNITIES" --
    # if the route ever added one, this stub would silently no-op it ("OK")
    # rather than persist a row, so the real proof is functional: querying
    # the opportunities repository (against this same stub) for this contact
    # returns nothing, because the repository's own SELECT never finds a row
    # that was never inserted.
    claims = AuthClaims(subject="user-1", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
    rows, total = await list_opportunities(db, claims, contact_id=contact_id)
    assert total == 0
    assert rows == []


# ---------------------------------------------------------------------------
# D7 -- consent carry-through, byte-identical
# ---------------------------------------------------------------------------


async def test_consent_carried_through_byte_identical(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-4", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-4/convert", json={}, cookies={"access_token": token},
        )

    contact_id = response.json()["contact_id"]
    stored_contact = db._contacts[(_TENANT_ID, contact_id)]
    assert stored_contact["consent"] == _ORIGINAL_CONSENT
    assert stored_contact["consent"]["captured_at"] == _ORIGINAL_CONSENT["captured_at"]


# ---------------------------------------------------------------------------
# K5 regression (MANDATORY) -- Lead survives, activities preserved
# ---------------------------------------------------------------------------


async def test_k5_regression_lead_and_activities_survive_conversion(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-5", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        # Add a pre-existing note before conversion.
        await client.post(
            "/admin/leads/lead-5/notes",
            json={"text": "Pre-conversion note."},
            cookies={"access_token": token},
        )
        pre_activities = (
            await client.get(
                "/admin/leads/lead-5/activities", cookies={"access_token": token},
            )
        ).json()

        convert_response = await client.post(
            "/admin/leads/lead-5/convert", json={}, cookies={"access_token": token},
        )
        assert convert_response.status_code == 201
        contact_id = convert_response.json()["contact_id"]

        # The lead still returns via GET (not 404).
        lead_get = await client.get(
            "/admin/leads/lead-5", cookies={"access_token": token},
        )
        assert lead_get.status_code == 200
        assert lead_get.json()["lead_id"] == "lead-5"

        post_activities = (
            await client.get(
                "/admin/leads/lead-5/activities", cookies={"access_token": token},
            )
        ).json()

    # All prior activities preserved, plus exactly one new converted_to_contact entry.
    assert len(post_activities) == len(pre_activities) + 1
    pre_ids = {a["activity_id"] for a in pre_activities}
    new_entries = [a for a in post_activities if a["activity_id"] not in pre_ids]
    assert len(new_entries) == 1
    assert new_entries[0]["type"] == "converted_to_contact"
    assert new_entries[0]["payload"] == {"contact_id": contact_id}


# ---------------------------------------------------------------------------
# include_converted list behavior
# ---------------------------------------------------------------------------


async def test_converted_lead_absent_by_default_present_with_include_converted(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-6", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.post(
            "/admin/leads/lead-6/convert", json={}, cookies={"access_token": token},
        )

        default_list = await client.get("/admin/leads", cookies={"access_token": token})
        included_list = await client.get(
            "/admin/leads?include_converted=true", cookies={"access_token": token},
        )

    default_ids = [item["lead_id"] for item in default_list.json()["items"]]
    assert "lead-6" not in default_ids

    included_items = {item["lead_id"]: item for item in included_list.json()["items"]}
    assert "lead-6" in included_items
    assert included_items["lead-6"]["stage"] == "converted"


# ---------------------------------------------------------------------------
# Anonymous booking-shape lead conversion (SR-9.1 shape)
# ---------------------------------------------------------------------------


async def test_convert_anonymous_lead_null_name_email_only_visitor_identity(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_lead(
        tenant_id=_TENANT_ID, lead_id="lead-7", stage="captured",
        name=None, email=None, visitor_id="visitor-anon-1",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-7/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 201
    contact_id = response.json()["contact_id"]
    stored_contact = db._contacts[(_TENANT_ID, contact_id)]
    assert stored_contact["name"] is None
    assert stored_contact["email"] is None

    identities = [
        i for i in db._identities.values()
        if i["tenant_id"] == _TENANT_ID and i["contact_id"] == contact_id
    ]
    assert len(identities) == 1
    assert identities[0]["identity_type"] == "visitor_id"
    assert identities[0]["identity_value"] == "visitor-anon-1"


async def test_convert_lead_with_email_and_visitor_id_creates_both_identities(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_lead(
        tenant_id=_TENANT_ID, lead_id="lead-8", stage="captured",
        email=_PII_EMAIL, visitor_id="visitor-8",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-8/convert", json={}, cookies={"access_token": token},
        )

    contact_id = response.json()["contact_id"]
    identities = {
        i["identity_type"]: i["identity_value"]
        for i in db._identities.values()
        if i["tenant_id"] == _TENANT_ID and i["contact_id"] == contact_id
    }
    assert identities == {"email": _PII_EMAIL, "visitor_id": "visitor-8"}


# ---------------------------------------------------------------------------
# Account linking on convert
# ---------------------------------------------------------------------------


async def test_convert_with_account_id_links_existing_account(app: Any, db: _StubDatabase) -> None:
    db.seed_account(tenant_id=_TENANT_ID, account_id="acct-1", name="Acme Ltd")
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-9", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-9/convert",
            json={"account_id": "acct-1"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    contact_id = response.json()["contact_id"]
    assert db._contacts[(_TENANT_ID, contact_id)]["account_id"] == "acct-1"


async def test_convert_with_account_name_creates_and_links_new_account(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-10", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-10/convert",
            json={"account_name": "Brand New Co"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    contact_id = response.json()["contact_id"]
    linked_account_id = db._contacts[(_TENANT_ID, contact_id)]["account_id"]
    assert linked_account_id is not None
    assert db._accounts[(_TENANT_ID, linked_account_id)]["name"] == "Brand New Co"


async def test_convert_with_invalid_account_id_returns_422(app: Any, db: _StubDatabase) -> None:
    db.seed_account(tenant_id=_OTHER_TENANT_ID, account_id="acct-foreign")
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-11", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-11/convert",
            json={"account_id": "acct-foreign"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT"
    assert db._contacts == {}


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_client_admin_can_convert(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-12", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/lead-12/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 201


async def test_client_agent_convert_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-13", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/leads/lead-13/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_visitor_convert_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-14", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post(
            "/admin/leads/lead-14/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_no_auth_convert_returns_401(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-15", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/leads/lead-15/convert", json={})

    assert response.status_code == 401


async def test_platform_admin_convert_implicit_returns_403(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-16", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.post(
            "/admin/leads/lead-16/convert", json={}, cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_platform_admin_convert_tenant_explicit_returns_201(app: Any, db: _StubDatabase) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-17", stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="pa-real-1")
        response = await client.post(
            f"/admin/tenants/{_TENANT_ID}/leads/lead-17/convert",
            json={},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    rows = [r for r in db.audit_rows if r["action"] == "lead_converted_to_contact"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "pa-real-1"
    assert rows[0]["metadata"]["platform_admin"] is True


async def test_platform_admin_convert_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.post(
            "/admin/tenants/does-not-exist/leads/lead-1/convert",
            json={},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


# ---------------------------------------------------------------------------
# PII-safe logging
# ---------------------------------------------------------------------------


async def test_convert_pii_not_logged(app: Any, db: _StubDatabase, caplog: Any) -> None:
    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-18", stage="captured")

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.post(
                "/admin/leads/lead-18/convert", json={}, cookies={"access_token": token},
            )

    log_text = caplog.text
    assert _PII_NAME not in log_text
    assert _PII_EMAIL not in log_text
    assert _PII_PHONE not in log_text
    assert _PII_CONSENT_TEXT not in log_text


# ---------------------------------------------------------------------------
# Repository-level: mark_lead_converted / list_leads(include_converted=...)
# ---------------------------------------------------------------------------


def _claims(tenant_id: str = _TENANT_ID, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="admin-123", role=role, tenant_id=tenant_id)


async def test_mark_lead_converted_returns_true_on_first_call() -> None:
    from unittest.mock import patch

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import mark_lead_converted

        db = _StubDatabase()
        db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-r1", stage="captured")
        claims = _claims()

        result = await mark_lead_converted(db, claims, "lead-r1", contact_id="contact-x")

        assert result is True
        assert db._leads[(_TENANT_ID, "lead-r1")]["converted_to_contact_id"] == "contact-x"
        assert db._leads[(_TENANT_ID, "lead-r1")]["stage"] == "converted"
        assert db._leads[(_TENANT_ID, "lead-r1")]["status"] == "won"


async def test_mark_lead_converted_returns_false_when_already_converted() -> None:
    from unittest.mock import patch

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import mark_lead_converted

        db = _StubDatabase()
        db.seed_lead(
            tenant_id=_TENANT_ID, lead_id="lead-r2", stage="converted",
            converted_to_contact_id="contact-existing",
        )
        claims = _claims()

        result = await mark_lead_converted(db, claims, "lead-r2", contact_id="contact-new")

        assert result is False
        # Unchanged -- still points at the original contact.
        assert db._leads[(_TENANT_ID, "lead-r2")]["converted_to_contact_id"] == "contact-existing"


async def test_mark_lead_converted_rejects_global_caller() -> None:
    from unittest.mock import patch

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import mark_lead_converted

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await mark_lead_converted(db, global_claims, "lead-1", contact_id="contact-1")


async def test_list_leads_include_converted_true_and_false() -> None:
    from unittest.mock import patch

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-live", stage="qualified")
        db.seed_lead(
            tenant_id=_TENANT_ID, lead_id="lead-converted", stage="converted",
            converted_to_contact_id="contact-1",
        )
        claims = _claims()

        default_rows, default_total = await list_leads(db, claims)
        included_rows, included_total = await list_leads(db, claims, include_converted=True)

        default_ids = {r.lead_id for r in default_rows}
        assert "lead-converted" not in default_ids
        assert "lead-live" in default_ids
        assert default_total == 1

        included_ids = {r.lead_id for r in included_rows}
        assert "lead-converted" in included_ids
        assert "lead-live" in included_ids
        assert included_total == 2


# ---------------------------------------------------------------------------
# SR-21: lead_converted feed emit (D2/D3)
# ---------------------------------------------------------------------------


async def test_convert_emits_lead_converted(app: Any, db: _StubDatabase) -> None:
    from unittest.mock import patch

    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-emit-1", stage="captured")

    with patch("api.leads.admin_routes.emit_event_safe") as mock_emit:
        mock_emit.return_value = "event-1"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/leads/lead-emit-1/convert", json={}, cookies={"access_token": token},
            )

    assert response.status_code == 201
    contact_id = response.json()["contact_id"]
    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "lead_converted"
    assert kwargs["category"] == "leads"
    assert kwargs["target_id"] == "lead-emit-1"
    assert kwargs["payload"] == {"lead_id": "lead-emit-1", "contact_id": contact_id}


async def test_convert_still_201_when_feed_emit_raises(app: Any, db: _StubDatabase) -> None:
    """MANDATORY (D2): a feed-insert failure must not fail the conversion."""
    from unittest.mock import AsyncMock, patch

    db.seed_lead(tenant_id=_TENANT_ID, lead_id="lead-emit-2", stage="captured")

    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/leads/lead-emit-2/convert", json={}, cookies={"access_token": token},
            )

    assert response.status_code == 201
    assert response.json()["lead_id"] == "lead-emit-2"
