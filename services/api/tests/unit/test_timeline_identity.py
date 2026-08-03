"""Unit tests for api.timeline.identity (SR-9.3 D2/D3).

Covers:
- resolve_contact_identities: collects visitor_id/email from
  contact_identities + the contact's own lead_id + any lead whose
  converted_to_contact_id points at it (D2). Missing/cross-tenant -> None.
- resolve_lead_identities: the lead's own lead_id + visitor_id/email where
  non-NULL. Missing/cross-tenant -> None.
- D3 (no inference): only physically-present values enter the set -- no
  domain/name matching, no transitive hops.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from common.auth import AuthClaims, Role

_TEST_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims(tenant_id: str = "tenant-abc", role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)


class _StubDatabase:
    """In-memory stub covering contacts, contact_identities, and leads."""

    def __init__(self) -> None:
        self._contacts: dict[tuple[str, str], dict[str, Any]] = {}
        self._identities: dict[tuple[str, str], dict[str, Any]] = {}
        self._leads: dict[tuple[str, str], dict[str, Any]] = {}

    def seed_contact(
        self, *, tenant_id: str, contact_id: str, lead_id: str | None = None,
    ) -> None:
        self._contacts[(tenant_id, contact_id)] = {
            "tenant_id": tenant_id, "contact_id": contact_id, "account_id": None,
            "lead_id": lead_id, "name": "Dana", "email": "dana@example.com",
            "phone": None, "consent": {"granted": True}, "owner_agent_id": None,
            "created_at": _NOW, "updated_at": _NOW,
        }

    def seed_identity(
        self, *, tenant_id: str, contact_id: str, identity_type: str, identity_value: str,
    ) -> None:
        identity_id = f"id-{len(self._identities)}"
        self._identities[(tenant_id, identity_id)] = {
            "tenant_id": tenant_id, "identity_id": identity_id, "contact_id": contact_id,
            "identity_type": identity_type, "identity_value": identity_value,
            "created_at": _NOW,
        }

    def seed_lead(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        visitor_id: str | None = None,
        email: str | None = None,
        converted_to_contact_id: str | None = None,
    ) -> None:
        self._leads[(tenant_id, lead_id)] = {
            "tenant_id": tenant_id, "lead_id": lead_id, "visitor_id": visitor_id,
            "name": None, "email": email, "phone": None, "status": "new",
            "stage": "captured", "qualification_score": None, "consent": {},
            "assigned_agent_id": None, "source": "widget", "created_at": _NOW,
            "updated_at": _NOW, "converted_to_contact_id": converted_to_contact_id,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM CONTACTS" in q and "CONTACT_ID = $2" in q:
            tenant_id, contact_id = args
            return self._contacts.get((tenant_id, contact_id))
        if "FROM LEADS" in q and "LEAD_ID = $2" in q:
            tenant_id, lead_id = args
            return self._leads.get((tenant_id, lead_id))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM CONTACT_IDENTITIES" in q:
            tenant_id, contact_id = args
            return [
                row for row in self._identities.values()
                if row["tenant_id"] == tenant_id and row["contact_id"] == contact_id
            ]
        if "FROM LEADS" in q and "CONVERTED_TO_CONTACT_ID = $2" in q:
            tenant_id, contact_id = args
            return [
                row for row in self._leads.values()
                if row["tenant_id"] == tenant_id
                and row.get("converted_to_contact_id") == contact_id
            ]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    async def close(self) -> None:
        pass


@pytest.fixture
def stub_db() -> _StubDatabase:
    return _StubDatabase()


# ---------------------------------------------------------------------------
# resolve_contact_identities
# ---------------------------------------------------------------------------


async def test_resolve_contact_identities_collects_visitor_and_email(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-1")
        stub_db.seed_identity(
            tenant_id="tenant-a", contact_id="contact-1",
            identity_type="visitor_id", identity_value="visitor-1",
        )
        stub_db.seed_identity(
            tenant_id="tenant-a", contact_id="contact-1",
            identity_type="email", identity_value="dana@example.com",
        )

        identities = await resolve_contact_identities(stub_db, _claims("tenant-a"), "contact-1")

        assert identities is not None
        assert identities.visitor_ids == ("visitor-1",)
        assert identities.emails == ("dana@example.com",)


async def test_resolve_contact_identities_multi_device_two_visitor_ids(
    stub_db: _StubDatabase,
) -> None:
    """D2 multi-device case: 2 visitor_id identities both enter the set."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-1")
        stub_db.seed_identity(
            tenant_id="tenant-a", contact_id="contact-1",
            identity_type="visitor_id", identity_value="visitor-device-1",
        )
        stub_db.seed_identity(
            tenant_id="tenant-a", contact_id="contact-1",
            identity_type="visitor_id", identity_value="visitor-device-2",
        )

        identities = await resolve_contact_identities(stub_db, _claims("tenant-a"), "contact-1")

        assert identities is not None
        assert set(identities.visitor_ids) == {"visitor-device-1", "visitor-device-2"}


async def test_resolve_contact_identities_includes_own_lead_id(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-1", lead_id="lead-origin")

        identities = await resolve_contact_identities(stub_db, _claims("tenant-a"), "contact-1")

        assert identities is not None
        assert "lead-origin" in identities.lead_ids


async def test_resolve_contact_identities_includes_converted_lead_belt_and_braces(
    stub_db: _StubDatabase,
) -> None:
    """D2 belt-and-braces: a lead whose converted_to_contact_id points at
    this contact enters the identity set even without contacts.lead_id set."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-1", lead_id=None)
        stub_db.seed_lead(
            tenant_id="tenant-a", lead_id="lead-converted",
            converted_to_contact_id="contact-1",
        )

        identities = await resolve_contact_identities(stub_db, _claims("tenant-a"), "contact-1")

        assert identities is not None
        assert "lead-converted" in identities.lead_ids


async def test_resolve_contact_identities_manually_created_contact_email_only(
    stub_db: _StubDatabase,
) -> None:
    """A manually-created contact (SR-9.2 D2, lead_id IS NULL) resolves via
    email identity alone -- the identity-first path does not depend on a
    lead existing."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-manual", lead_id=None)
        stub_db.seed_identity(
            tenant_id="tenant-a", contact_id="contact-manual",
            identity_type="email", identity_value="manual@example.com",
        )

        identities = await resolve_contact_identities(stub_db, _claims("tenant-a"), "contact-manual")

        assert identities is not None
        assert identities.lead_ids == ()
        assert identities.emails == ("manual@example.com",)


async def test_resolve_contact_identities_missing_returns_none(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        identities = await resolve_contact_identities(stub_db, _claims("tenant-a"), "does-not-exist")
        assert identities is None


async def test_resolve_contact_identities_cross_tenant_returns_none(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-1")

        identities = await resolve_contact_identities(stub_db, _claims("tenant-b"), "contact-1")
        assert identities is None


async def test_resolve_contact_identities_no_inference_across_tenants(
    stub_db: _StubDatabase,
) -> None:
    """D3: a second contact sharing the SAME email domain/name in a
    DIFFERENT tenant never bleeds into this contact's identity set --
    tenant scoping already prevents it structurally."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_contact_identities

        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-1")
        stub_db.seed_identity(
            tenant_id="tenant-a", contact_id="contact-1",
            identity_type="email", identity_value="dana@acme.example",
        )
        # Same domain, different contact, same tenant -- must NOT bleed in.
        stub_db.seed_contact(tenant_id="tenant-a", contact_id="contact-2")
        stub_db.seed_identity(
            tenant_id="tenant-a", contact_id="contact-2",
            identity_type="email", identity_value="other@acme.example",
        )

        identities = await resolve_contact_identities(stub_db, _claims("tenant-a"), "contact-1")

        assert identities is not None
        assert identities.emails == ("dana@acme.example",)


# ---------------------------------------------------------------------------
# resolve_lead_identities
# ---------------------------------------------------------------------------


async def test_resolve_lead_identities_collects_own_fields(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_lead_identities

        stub_db.seed_lead(
            tenant_id="tenant-a", lead_id="lead-1",
            visitor_id="visitor-1", email="lead1@example.com",
        )

        identities = await resolve_lead_identities(stub_db, _claims("tenant-a"), "lead-1")

        assert identities is not None
        assert identities.lead_ids == ("lead-1",)
        assert identities.visitor_ids == ("visitor-1",)
        assert identities.emails == ("lead1@example.com",)


async def test_resolve_lead_identities_omits_null_fields(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_lead_identities

        stub_db.seed_lead(tenant_id="tenant-a", lead_id="lead-2", visitor_id=None, email=None)

        identities = await resolve_lead_identities(stub_db, _claims("tenant-a"), "lead-2")

        assert identities is not None
        assert identities.visitor_ids == ()
        assert identities.emails == ()
        assert identities.lead_ids == ("lead-2",)


async def test_resolve_lead_identities_converted_lead_does_not_widen(
    stub_db: _StubDatabase,
) -> None:
    """D2: a converted lead's identity set is still JUST its own fields --
    resolve_lead_identities never reaches into the contact's fuller set."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_lead_identities

        stub_db.seed_lead(
            tenant_id="tenant-a", lead_id="lead-converted",
            visitor_id="visitor-1", email="lead@example.com",
            converted_to_contact_id="contact-9",
        )

        identities = await resolve_lead_identities(stub_db, _claims("tenant-a"), "lead-converted")

        assert identities is not None
        assert identities.lead_ids == ("lead-converted",)
        assert identities.visitor_ids == ("visitor-1",)


async def test_resolve_lead_identities_missing_returns_none(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_lead_identities

        identities = await resolve_lead_identities(stub_db, _claims("tenant-a"), "does-not-exist")
        assert identities is None


async def test_resolve_lead_identities_cross_tenant_returns_none(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.identity import resolve_lead_identities

        stub_db.seed_lead(tenant_id="tenant-a", lead_id="lead-1")

        identities = await resolve_lead_identities(stub_db, _claims("tenant-b"), "lead-1")
        assert identities is None
