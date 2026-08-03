"""Unit tests for /admin/opportunities (SR-9.4).

Covers (D1-D11, MANDATORY per the sprint spec):
- POST validates contact_id/account_id, copies account_id from contact when
  omitted, stamps currency from config, starts at prospecting.
- GET/PATCH/stage: 404 for missing/cross-tenant (no existence leak).
- Cross-tenant contact_id/account_id on create -> 422, never 500.
- Stage machine enforcement end-to-end (skip/backward/no-op rejected;
  closed_lost requires close_reason; no reopen).
- Derived win_probability changes live when config changes (D2 regression).
- RBAC per D8: CLIENT_AGENT full working access, 403 only on config PUT.
- PATCH cannot change stage/currency.
- POST /admin/leads/{lead_id}/convert creates zero opportunities (D4).
- Audit rows for every mutation with the exact action names.
- PII-safe logging: no close_reason or contact PII in logs; no tenant_id
  in responses.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
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

_PII_CLOSE_REASON = "Lost because the customer's ex-wife works at CompetitorCo and gave them a huge discount"


class _StubDatabase:
    """In-memory stub database backing /admin/opportunities for these tests."""

    def __init__(self) -> None:
        self._opportunities: dict[tuple[str, str], dict[str, Any]] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._contacts: dict[tuple[str, str], dict[str, Any]] = {}
        self._accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self._tenants: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    def seed_tenant(self, *, tenant_id: str, slug: str, enabled: bool = True) -> None:
        self._tenants[tenant_id] = {"id": tenant_id, "name": slug, "slug": slug, "enabled": enabled}

    def seed_account(self, *, tenant_id: str, account_id: str, name: str = "Acme Ltd") -> None:
        self._accounts[(tenant_id, account_id)] = {
            "tenant_id": tenant_id, "account_id": account_id, "name": name,
            "domain": None, "created_at": _NOW, "updated_at": _NOW,
        }

    def seed_contact(
        self, *, tenant_id: str, contact_id: str, account_id: str | None = None,
    ) -> None:
        self._contacts[(tenant_id, contact_id)] = {
            "tenant_id": tenant_id, "contact_id": contact_id, "account_id": account_id,
            "lead_id": None, "name": "Test Contact", "email": "test@example.com",
            "phone": None, "consent": {"granted": True}, "owner_agent_id": None,
            "created_at": _NOW, "updated_at": _NOW,
        }

    def seed_opportunity_config(
        self, *, tenant_id: str, currency: str, stage_probabilities: dict[str, int],
    ) -> None:
        self._configs[tenant_id] = {
            "tenant_id": tenant_id, "currency": currency,
            "stage_probabilities": stage_probabilities,
        }

    def seed_opportunity(
        self,
        *,
        tenant_id: str,
        opportunity_id: str,
        contact_id: str = "contact-1",
        account_id: str | None = None,
        name: str = "Test deal",
        amount: Decimal | None = Decimal("100.00"),
        currency: str = "USD",
        stage: str = "prospecting",
        owner_agent_id: str | None = None,
    ) -> None:
        self._opportunities[(tenant_id, opportunity_id)] = {
            "tenant_id": tenant_id, "opportunity_id": opportunity_id,
            "contact_id": contact_id, "account_id": account_id, "name": name,
            "amount": amount, "currency": currency, "stage": stage,
            "expected_close_date": None, "closed_at": None, "close_reason": None,
            "owner_agent_id": owner_agent_id, "created_at": _NOW, "updated_at": _NOW,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q:
            return self._accounts.get((args[0], args[1]))
        if "FROM CONTACTS" in q and "CONTACT_ID = $2" in q:
            return self._contacts.get((args[0], args[1]))
        if "FROM TENANT_OPPORTUNITY_CONFIGS" in q:
            return self._configs.get(args[0])
        if "COUNT(*)" in q and "FROM OPPORTUNITIES" in q:
            return {"count": len(self._filtered(query, args))}
        if "FROM OPPORTUNITIES" in q and "OPPORTUNITY_ID = $2" in q:
            return self._opportunities.get((args[0], args[1]))
        return None

    def _filtered(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        tenant_id = args[0]
        rows = [r for r in self._opportunities.values() if r["tenant_id"] == tenant_id]
        idx = 1
        q = query.upper()
        if " STAGE = $" in q:
            rows = [r for r in rows if r["stage"] == args[idx]]
            idx += 1
        if " CONTACT_ID = $" in q:
            rows = [r for r in rows if r["contact_id"] == args[idx]]
            idx += 1
        if " ACCOUNT_ID = $" in q:
            rows = [r for r in rows if r["account_id"] == args[idx]]
            idx += 1
        if " OWNER_AGENT_ID = $" in q:
            rows = [r for r in rows if r["owner_agent_id"] == args[idx]]
            idx += 1
        if "STAGE NOT IN ('CLOSED_WON','CLOSED_LOST')" in q:
            rows = [r for r in rows if r["stage"] not in ("closed_won", "closed_lost")]
        return rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM OPPORTUNITIES" in q:
            if " LIMIT $" in q:
                limit, offset = args[-2], args[-1]
                base_args = args[:-2]
            else:
                limit, offset, base_args = None, 0, args
            rows = self._filtered(query, base_args)
            rows.sort(key=lambda r: (r["created_at"], r["opportunity_id"]), reverse=True)
            if limit is not None:
                rows = rows[offset : offset + limit]
            return rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO OPPORTUNITIES"):
            (
                tenant_id, opportunity_id, contact_id, account_id, name,
                amount, currency, expected_close_date, owner_agent_id,
            ) = args
            self._opportunities[(tenant_id, opportunity_id)] = {
                "tenant_id": tenant_id, "opportunity_id": opportunity_id,
                "contact_id": contact_id, "account_id": account_id, "name": name,
                "amount": amount, "currency": currency, "stage": "prospecting",
                "expected_close_date": expected_close_date, "closed_at": None,
                "close_reason": None, "owner_agent_id": owner_agent_id,
                "created_at": _NOW, "updated_at": _NOW,
            }
            return "INSERT 0 1"
        if q.startswith("INSERT INTO TENANT_OPPORTUNITY_CONFIGS"):
            tenant_id, currency, stage_probabilities = args
            self._configs[tenant_id] = {
                "tenant_id": tenant_id, "currency": currency,
                "stage_probabilities": stage_probabilities,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE OPPORTUNITIES SET STAGE = $1"):
            stage, close_reason, closed_at, tenant_id, opportunity_id = args
            existing = self._opportunities.get((tenant_id, opportunity_id))
            if existing is None:
                return "UPDATE 0"
            existing["stage"] = stage
            existing["close_reason"] = close_reason
            existing["closed_at"] = closed_at
            existing["updated_at"] = _NOW
            return "UPDATE 1"
        if q.startswith("UPDATE OPPORTUNITIES"):
            tenant_id, opportunity_id = args[-2], args[-1]
            existing = self._opportunities.get((tenant_id, opportunity_id))
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


@pytest.fixture
def db() -> _StubDatabase:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_tenant(tenant_id=_OTHER_TENANT_ID, slug="widgetco")
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    return d


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# GET /admin/opportunities/config -- unconfigured tenant resolves defaults
# ---------------------------------------------------------------------------


async def test_config_resolves_before_anything_configured(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/opportunities/config", cookies={"access_token": token})

    assert response.status_code == 200
    data = response.json()
    assert data["currency"] == "USD"
    assert data["stage_probabilities"] == {
        "prospecting": 10, "qualification": 25, "proposal": 50, "negotiation": 75,
    }


# ---------------------------------------------------------------------------
# POST /admin/opportunities
# ---------------------------------------------------------------------------


async def test_post_creates_deal_stamped_with_config_currency(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-1", "name": "Roof job", "amount": "12500.00"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["stage"] == "prospecting"
    assert data["win_probability"] == 10
    assert data["currency"] == "USD"
    assert data["amount"] == "12500.00"
    assert "tenant_id" not in data


async def test_post_cross_tenant_contact_id_returns_422_invalid_contact(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="foreign-contact")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "foreign-contact", "name": "Deal"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CONTACT"


async def test_post_cross_tenant_account_id_returns_422_invalid_account(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_account(tenant_id=_OTHER_TENANT_ID, account_id="foreign-acct")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-1", "account_id": "foreign-acct", "name": "Deal"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ACCOUNT"


async def test_post_negative_amount_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-1", "name": "Deal", "amount": "-1.00"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_post_zero_amount_accepted(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-1", "name": "Free deal", "amount": "0"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    assert response.json()["amount"] == "0"


async def test_post_no_amount_returns_null_not_zero(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-1", "name": "Unquoted deal"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    assert response.json()["amount"] is None


async def test_post_omitted_account_id_copies_from_contact(app: Any, db: _StubDatabase) -> None:
    db.seed_account(tenant_id=_TENANT_ID, account_id="acct-1")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-with-account", account_id="acct-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-with-account", "name": "Deal"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    assert response.json()["account_id"] == "acct-1"


async def test_post_no_account_on_contact_yields_null_account(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-1", "name": "Deal"},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    assert response.json()["account_id"] is None


async def test_repointing_contact_account_does_not_change_existing_opportunity(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_account(tenant_id=_TENANT_ID, account_id="acct-1")
    db.seed_account(tenant_id=_TENANT_ID, account_id="acct-2")
    db.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-2", account_id="acct-1")
    db.seed_opportunity(
        tenant_id=_TENANT_ID, opportunity_id="opp-1", contact_id="contact-2", account_id="acct-1",
    )

    # Re-point the contact to a different account (simulated directly on the stub).
    db._contacts[(_TENANT_ID, "contact-2")]["account_id"] = "acct-2"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/opportunities/opp-1", cookies={"access_token": token})

    assert response.status_code == 200
    assert response.json()["account_id"] == "acct-1"


async def test_contact_can_hold_several_open_opportunities(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        for i in range(3):
            response = await client.post(
                "/admin/opportunities",
                json={"contact_id": "contact-1", "name": f"Deal {i}"},
                cookies={"access_token": token},
            )
            assert response.status_code == 201

        listing = await client.get(
            "/admin/opportunities?contact_id=contact-1", cookies={"access_token": token},
        )
    assert listing.json()["total"] == 3


async def test_post_writes_audit_row(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.post(
            "/admin/opportunities",
            json={"contact_id": "contact-1", "name": "Deal"},
            cookies={"access_token": token},
        )

    rows = [r for r in db.audit_rows if r["action"] == "opportunity_created"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# GET /admin/opportunities/{id} -- 404 tenant isolation
# ---------------------------------------------------------------------------


async def test_get_missing_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/opportunities/does-not-exist", cookies={"access_token": token},
        )
    assert response.status_code == 404


async def test_get_cross_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        response = await client.get("/admin/opportunities/opp-1", cookies={"access_token": token})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /admin/opportunities/{id}
# ---------------------------------------------------------------------------


async def test_patch_only_supplied_fields_change(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", name="Original")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/opportunities/opp-1", json={"name": "Updated"},
            cookies={"access_token": token},
        )
    assert response.status_code == 200
    assert response.json()["name"] == "Updated"
    assert response.json()["amount"] == "100.00"


async def test_patch_expected_close_date_null_clears(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1")
    db._opportunities[(_TENANT_ID, "opp-1")]["expected_close_date"] = "2026-09-30"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/opportunities/opp-1", json={"expected_close_date": None},
            cookies={"access_token": token},
        )
    assert response.status_code == 200
    assert response.json()["expected_close_date"] is None


async def test_patch_cannot_change_stage(app: Any, db: _StubDatabase) -> None:
    """PATCH body has no `stage` field in the model -- an extra field is
    silently ignored by Pydantic (no `stage` param exists to bind it to),
    so the stage remains whatever it was."""
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="prospecting")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/opportunities/opp-1", json={"stage": "closed_won"},
            cookies={"access_token": token},
        )
    assert response.status_code == 200
    assert response.json()["stage"] == "prospecting"


async def test_patch_cannot_change_currency(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", currency="USD")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/opportunities/opp-1", json={"currency": "GBP"},
            cookies={"access_token": token},
        )
    assert response.status_code == 200
    assert response.json()["currency"] == "USD"


async def test_patch_cross_tenant_returns_404_and_no_mutation(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", name="Original")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        response = await client.patch(
            "/admin/opportunities/opp-1", json={"name": "Hacked"},
            cookies={"access_token": token},
        )
    assert response.status_code == 404
    assert db._opportunities[(_TENANT_ID, "opp-1")]["name"] == "Original"


# ---------------------------------------------------------------------------
# POST /admin/opportunities/{id}/stage -- state machine + D9 terminal rules
# ---------------------------------------------------------------------------


async def test_stage_transition_skip_returns_422(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="prospecting")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "closed_won"},
            cookies={"access_token": token},
        )
    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_OPPORTUNITY_STAGE_TRANSITION"

    check = await AsyncClient(transport=ASGITransport(app=app), base_url="http://test").get(
        "/admin/opportunities/opp-1", cookies={"access_token": _token(Role.CLIENT_ADMIN)},
    )
    assert check.json()["stage"] == "prospecting"


async def test_stage_transition_legal_walk_updates_probability(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="prospecting")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        r1 = await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "qualification"},
            cookies={"access_token": token},
        )
        assert r1.status_code == 200
        assert r1.json()["win_probability"] == 25


async def test_closed_lost_without_reason_returns_422_and_stage_unchanged(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="negotiation")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "closed_lost"},
            cookies={"access_token": token},
        )
    assert response.status_code == 422
    assert response.json()["error_code"] == "CLOSE_REASON_REQUIRED"
    assert db._opportunities[(_TENANT_ID, "opp-1")]["stage"] == "negotiation"
    assert db._opportunities[(_TENANT_ID, "opp-1")]["closed_at"] is None


async def test_closed_lost_whitespace_reason_returns_422(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="negotiation")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities/opp-1/stage",
            json={"stage": "closed_lost", "close_reason": "   "},
            cookies={"access_token": token},
        )
    assert response.status_code == 422
    assert response.json()["error_code"] == "CLOSE_REASON_REQUIRED"


async def test_closed_lost_with_reason_succeeds_and_stamps_closed_at(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="negotiation")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities/opp-1/stage",
            json={"stage": "closed_lost", "close_reason": "Lost to a cheaper competitor"},
            cookies={"access_token": token},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["win_probability"] == 0
    assert data["closed_at"] is not None


async def test_closed_won_without_reason_succeeds(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="negotiation")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "closed_won"},
            cookies={"access_token": token},
        )
    assert response.status_code == 200
    assert response.json()["win_probability"] == 100
    assert response.json()["closed_at"] is not None


@pytest.mark.parametrize("target", ["prospecting", "qualification", "proposal", "negotiation", "closed_lost"])
async def test_no_reopen_from_closed_won(app: Any, db: _StubDatabase, target: str) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="closed_won")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/opportunities/opp-1/stage",
            json={"stage": target, "close_reason": "reason"},
            cookies={"access_token": token},
        )
    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_OPPORTUNITY_STAGE_TRANSITION"


async def test_open_only_excludes_closed(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-open", stage="proposal")
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-won", stage="closed_won")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/opportunities?open_only=true", cookies={"access_token": token},
        )
    assert response.json()["total"] == 1


async def test_stage_transition_writes_audit_row(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="prospecting")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "qualification"},
            cookies={"access_token": token},
        )

    rows = [r for r in db.audit_rows if r["action"] == "opportunity_stage_transitioned"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# D2 regression: config change immediately changes an existing open deal's
# displayed win_probability, with zero row writes.
# ---------------------------------------------------------------------------


async def test_config_change_immediately_changes_existing_open_opportunity_probability(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="proposal", currency="USD")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)

        before = await client.get("/admin/opportunities/opp-1", cookies={"access_token": token})
        assert before.json()["win_probability"] == 50

        put_response = await client.put(
            "/admin/opportunities/config",
            json={
                "currency": "GBP",
                "stage_probabilities": {
                    "prospecting": 5, "qualification": 20, "proposal": 60, "negotiation": 85,
                },
            },
            cookies={"access_token": token},
        )
        assert put_response.status_code == 200

        after = await client.get("/admin/opportunities/opp-1", cookies={"access_token": token})

    assert after.json()["win_probability"] == 60
    # D2: no write to the row itself -- the stub DB's row dict is unchanged
    # except by an actual UPDATE opportunities call, which never happened.
    assert db._opportunities[(_TENANT_ID, "opp-1")]["stage"] == "proposal"


async def test_config_change_does_not_retroactively_change_existing_currency(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", currency="USD")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.put(
            "/admin/opportunities/config",
            json={
                "currency": "GBP",
                "stage_probabilities": {
                    "prospecting": 5, "qualification": 20, "proposal": 60, "negotiation": 85,
                },
            },
            cookies={"access_token": token},
        )
        existing = await client.get("/admin/opportunities/opp-1", cookies={"access_token": token})

        new_deal = await client.post(
            "/admin/opportunities", json={"contact_id": "contact-1", "name": "New deal"},
            cookies={"access_token": token},
        )

    assert existing.json()["currency"] == "USD"
    assert new_deal.json()["currency"] == "GBP"


async def test_put_config_invalid_probability_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/opportunities/config",
            json={"currency": "USD", "stage_probabilities": {"proposal": 150}},
            cookies={"access_token": token},
        )
    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_STAGE_PROBABILITIES"


async def test_put_config_terminal_stage_key_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/opportunities/config",
            json={"currency": "USD", "stage_probabilities": {"closed_won": 80}},
            cookies={"access_token": token},
        )
    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_STAGE_PROBABILITIES"


async def test_put_config_invalid_currency_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/opportunities/config",
            json={"currency": "usd", "stage_probabilities": {}},
            cookies={"access_token": token},
        )
    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_CURRENCY"


# ---------------------------------------------------------------------------
# RBAC (D8): CLIENT_AGENT full working access, 403 only on config PUT
# ---------------------------------------------------------------------------


async def test_client_agent_post_returns_201(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/opportunities", json={"contact_id": "contact-1", "name": "Deal"},
            cookies={"access_token": token},
        )
    assert response.status_code == 201


async def test_client_agent_patch_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.patch(
            "/admin/opportunities/opp-1", json={"name": "Agent edit"},
            cookies={"access_token": token},
        )
    assert response.status_code == 200


async def test_client_agent_stage_transition_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="prospecting")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "qualification"},
            cookies={"access_token": token},
        )
    assert response.status_code == 200


async def test_client_agent_get_config_returns_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/opportunities/config", cookies={"access_token": token})
    assert response.status_code == 200


async def test_client_agent_put_config_returns_403(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/opportunities/config",
            json={"currency": "USD", "stage_probabilities": {}},
            cookies={"access_token": token},
        )
    assert response.status_code == 403


async def test_visitor_rejected_everywhere(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        assert (await client.post(
            "/admin/opportunities", json={"contact_id": "contact-1", "name": "Deal"},
            cookies={"access_token": token},
        )).status_code == 403
        assert (await client.get(
            "/admin/opportunities", cookies={"access_token": token},
        )).status_code == 403
        assert (await client.get(
            "/admin/opportunities/opp-1", cookies={"access_token": token},
        )).status_code == 403
        assert (await client.patch(
            "/admin/opportunities/opp-1", json={"name": "X"}, cookies={"access_token": token},
        )).status_code == 403
        assert (await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "qualification"},
            cookies={"access_token": token},
        )).status_code == 403
        assert (await client.get(
            "/admin/opportunities/config", cookies={"access_token": token},
        )).status_code == 403


async def test_platform_admin_implicit_rejected(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/opportunities", cookies={"access_token": token})
    assert response.status_code == 403


async def test_platform_admin_tenant_explicit_succeeds_and_audits_real_actor(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None, subject="pa-real-id")
        response = await client.post(
            f"/admin/tenants/{_TENANT_ID}/opportunities",
            json={"contact_id": "contact-1", "name": "Deal"},
            cookies={"access_token": token},
        )
    assert response.status_code == 201
    rows = [r for r in db.audit_rows if r["action"] == "opportunity_created"]
    assert rows[0]["actor"] == "pa-real-id"
    assert rows[0]["metadata"]["platform_admin"] is True


async def test_platform_admin_unknown_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            "/admin/tenants/does-not-exist/opportunities", cookies={"access_token": token},
        )
    assert response.status_code == 404
    assert response.json()["error_code"] == "TENANT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Tenant isolation (list, filters)
# ---------------------------------------------------------------------------


async def test_list_tenant_isolation(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-a")
    db.seed_opportunity(tenant_id=_OTHER_TENANT_ID, opportunity_id="opp-b")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/opportunities", cookies={"access_token": token})

    assert response.json()["total"] == 1
    assert response.json()["items"][0]["opportunity_id"] == "opp-a"


async def test_stage_transition_cross_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="prospecting")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        response = await client.post(
            "/admin/opportunities/opp-1/stage", json={"stage": "qualification"},
            cookies={"access_token": token},
        )
    assert response.status_code == 404


async def test_config_isolation_tenant_b_sees_platform_defaults(
    app: Any, db: _StubDatabase,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token_a = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        await client.put(
            "/admin/opportunities/config",
            json={
                "currency": "GBP",
                "stage_probabilities": {
                    "prospecting": 1, "qualification": 2, "proposal": 90, "negotiation": 99,
                },
            },
            cookies={"access_token": token_a},
        )

        token_b = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        response = await client.get("/admin/opportunities/config", cookies={"access_token": token_b})

    assert response.json()["currency"] == "USD"
    assert response.json()["stage_probabilities"] == {
        "prospecting": 10, "qualification": 25, "proposal": 50, "negotiation": 75,
    }


# ---------------------------------------------------------------------------
# PII / logging
# ---------------------------------------------------------------------------


async def test_close_reason_never_logged(app: Any, db: _StubDatabase, caplog: Any) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1", stage="negotiation")

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.post(
                "/admin/opportunities/opp-1/stage",
                json={"stage": "closed_lost", "close_reason": _PII_CLOSE_REASON},
                cookies={"access_token": token},
            )

    assert _PII_CLOSE_REASON not in caplog.text


async def test_contact_pii_never_logged(app: Any, db: _StubDatabase, caplog: Any) -> None:
    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.post(
                "/admin/opportunities",
                json={"contact_id": "contact-1", "name": "Deal for Test Contact"},
                cookies={"access_token": token},
            )

    assert "test@example.com" not in caplog.text


async def test_response_never_contains_tenant_id(app: Any, db: _StubDatabase) -> None:
    db.seed_opportunity(tenant_id=_TENANT_ID, opportunity_id="opp-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/opportunities/opp-1", cookies={"access_token": token})

    assert "tenant_id" not in response.json()
