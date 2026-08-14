"""Unit tests for POST /public/leads.

Covers:
- Consent granted=true -> 201 {lead_id, status:"new"}, create_lead called once.
- Consent granted=false -> 422 CONSENT_REQUIRED, create_lead NOT called.
- Consent omitted -> 422 CONSENT_REQUIRED, create_lead NOT called.
- Body tenant_id/visitor_id ignored (only from claims).
- Missing/blank name or email, or email without "@" -> 422.
- No Authorization header -> 401.
- Non-visitor token (e.g., admin) -> 403 NOT_A_VISITOR.
- PII not in logs (name, email, phone).
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"

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
    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        return None

    async def execute(self, query: str, *args: object) -> str:
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


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app(db: Any = None) -> Any:
    _reset_settings()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()
        if db is not None:
            app.state.db = db
        app.state.redis = _StubRedis()
        app.state.cache = InMemoryCache()
        return app


def _create_visitor_token(tenant_id: str = _TENANT_ID, visitor_id: str = "visitor-123") -> str:
    """Create a visitor JWT token."""
    claims = AuthClaims(subject=visitor_id, role=Role.VISITOR, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


def _create_admin_token(tenant_id: str = _TENANT_ID) -> str:
    """Create an admin JWT token (non-visitor)."""
    claims = AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_post_leads_consent_granted_returns_201() -> None:
    """POST /public/leads with consent.granted=true -> 201 {lead_id, status:"new"}."""
    db = _StubDatabase()
    app = _build_app(db)

    # Mock create_lead to return a lead_id
    mock_create_lead = AsyncMock(return_value="abc123def456")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "+1555123456",
                "consent": {
                    "granted": True,
                    "purpose": "contact",
                    "text": "I agree to be contacted.",
                },
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    data = response.json()
    assert data["lead_id"] == "abc123def456"
    assert data["status"] == "new"
    # Ensure no PII in response
    assert "email" not in data
    assert "name" not in data
    assert "tenant_id" not in data


async def test_post_leads_consent_false_returns_422_and_no_call() -> None:
    """POST /public/leads with consent.granted=false -> 422, create_lead not called."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="abc123def456")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "consent": {
                    "granted": False,
                    "purpose": "contact",
                    "text": "I do not agree.",
                },
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 422
    data = response.json()
    assert data["error_code"] == "CONSENT_REQUIRED"
    # create_lead should not have been called
    mock_create_lead.assert_not_awaited()


async def test_post_leads_consent_omitted_returns_422_and_no_call() -> None:
    """POST /public/leads with consent omitted -> 422 CONSENT_REQUIRED, create_lead not called."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="abc123def456")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Jane Doe",
                "email": "jane@example.com",
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 422
    data = response.json()
    assert data["error_code"] == "CONSENT_REQUIRED"
    mock_create_lead.assert_not_awaited()


async def test_post_leads_calls_create_lead_with_claims_visitor_id() -> None:
    """create_lead is called with visitor_id from claims.subject."""
    db = _StubDatabase()
    app = _build_app(db)

    visitor_id = "visitor-456"
    mock_create_lead = AsyncMock(return_value="abc123def456")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token(visitor_id=visitor_id)
            body = {
                "name": "Jane",
                "email": "jane@example.com",
                "consent": {
                    "granted": True,
                    "purpose": "contact",
                    "text": "OK",
                },
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    # Verify create_lead was called with visitor_id from claims
    mock_create_lead.assert_awaited_once()
    _, kwargs = mock_create_lead.call_args
    assert kwargs["visitor_id"] == visitor_id


async def test_post_leads_body_tenant_id_ignored() -> None:
    """Body tenant_id is ignored; only claims.tenant_id is used."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="abc123def456")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token(tenant_id="tenant-real")
            body = {
                "name": "Jane",
                "email": "jane@example.com",
                "tenant_id": "tenant-fake",  # Should be ignored
                "consent": {
                    "granted": True,
                    "purpose": "contact",
                    "text": "OK",
                },
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201


async def test_post_leads_missing_name_returns_422() -> None:
    """Missing name -> 422."""
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        visitor_token = _create_visitor_token()
        body = {
            "email": "jane@example.com",
            "consent": {
                "granted": True,
                "purpose": "contact",
                "text": "OK",
            },
        }

        response = await client.post(
            "/public/leads",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_leads_blank_name_returns_422() -> None:
    """Blank name -> 422."""
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        visitor_token = _create_visitor_token()
        body = {
            "name": "",
            "email": "jane@example.com",
            "consent": {
                "granted": True,
                "purpose": "contact",
                "text": "OK",
            },
        }

        response = await client.post(
            "/public/leads",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_leads_missing_email_returns_422() -> None:
    """Missing email -> 422."""
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        visitor_token = _create_visitor_token()
        body = {
            "name": "Jane",
            "consent": {
                "granted": True,
                "purpose": "contact",
                "text": "OK",
            },
        }

        response = await client.post(
            "/public/leads",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_leads_blank_email_returns_422() -> None:
    """Blank email -> 422."""
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        visitor_token = _create_visitor_token()
        body = {
            "name": "Jane",
            "email": "",
            "consent": {
                "granted": True,
                "purpose": "contact",
                "text": "OK",
            },
        }

        response = await client.post(
            "/public/leads",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_leads_email_without_at_returns_422() -> None:
    """Email without '@' -> 422."""
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        visitor_token = _create_visitor_token()
        body = {
            "name": "Jane",
            "email": "janedomain.com",  # missing @
            "consent": {
                "granted": True,
                "purpose": "contact",
                "text": "OK",
            },
        }

        response = await client.post(
            "/public/leads",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_leads_no_authorization_returns_401() -> None:
    """No Authorization header -> 401."""
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {
            "name": "Jane",
            "email": "jane@example.com",
            "consent": {
                "granted": True,
                "purpose": "contact",
                "text": "OK",
            },
        }

        response = await client.post(
            "/public/leads",
            json=body,
        )

    assert response.status_code == 401


async def test_post_leads_admin_token_returns_403_not_a_visitor() -> None:
    """Admin (non-visitor) token -> 403 NOT_A_VISITOR."""
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        admin_token = _create_admin_token()
        body = {
            "name": "Jane",
            "email": "jane@example.com",
            "consent": {
                "granted": True,
                "purpose": "contact",
                "text": "OK",
            },
        }

        response = await client.post(
            "/public/leads",
            json=body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert response.status_code == 403
    data = response.json()
    assert data["error_code"] == "NOT_A_VISITOR"


async def test_post_leads_pii_not_in_logs(caplog: Any) -> None:
    """Verify that email, name, phone are not logged."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="abc123def456")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        with caplog.at_level(logging.DEBUG):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                visitor_token = _create_visitor_token()
                body = {
                    "name": "Jane Doe",
                    "email": "jane@example.com",
                    "phone": "+1555123456",
                    "consent": {
                        "granted": True,
                        "purpose": "contact",
                        "text": "I agree.",
                    },
                }

                await client.post(
                    "/public/leads",
                    json=body,
                    headers={"Authorization": f"Bearer {visitor_token}"},
                )

    # Check that PII is not in logs
    log_text = caplog.text
    assert "jane@example.com" not in log_text
    assert "Jane Doe" not in log_text
    assert "+1555123456" not in log_text


async def test_post_leads_no_default_source() -> None:
    """When source is not provided in body, it defaults to 'widget'."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="abc123def456")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Jane",
                "email": "jane@example.com",
                "consent": {
                    "granted": True,
                    "purpose": "contact",
                    "text": "OK",
                },
                # no source provided
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    mock_create_lead.assert_awaited_once()
    _, kwargs = mock_create_lead.call_args
    assert kwargs["source"] == "widget"


# ---------------------------------------------------------------------------
# crm.sync_lead enqueue-on-capture (S7.4 decision 4)
# ---------------------------------------------------------------------------


async def test_post_leads_enqueues_crm_sync_with_trusted_ids() -> None:
    """After a successful capture, crm.sync_lead is enqueued with the trusted
    tenant_id + lead_id (never from the request body)."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-crm-1")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead") as mock_sync_lead,
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token(tenant_id="tenant-real")
            body = {
                "name": "Jane",
                "email": "jane@example.com",
                "tenant_id": "tenant-fake",
                "consent": {
                    "granted": True,
                    "purpose": "contact",
                    "text": "OK",
                },
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    mock_sync_lead.delay.assert_called_once()
    _, kwargs = mock_sync_lead.delay.call_args
    assert kwargs["tenant_id"] == "tenant-real"
    assert kwargs["lead_id"] == "lead-crm-1"


async def test_post_leads_still_201_when_enqueue_raises() -> None:
    """An enqueue failure must not fail capture -- it is wrapped and logged."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-crm-2")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead") as mock_sync_lead,
        patch("api.leads.routes.classify_lead_email"),
    ):
        mock_sync_lead.delay.side_effect = RuntimeError("broker unavailable")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Jane",
                "email": "jane@example.com",
                "consent": {
                    "granted": True,
                    "purpose": "contact",
                    "text": "OK",
                },
            }

            response = await client.post(
                "/public/leads",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    data = response.json()
    assert data["lead_id"] == "lead-crm-2"


async def test_post_leads_enqueue_failure_logs_crm_enqueue_failed(caplog: Any) -> None:
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-crm-3")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.sync_lead") as mock_sync_lead,
        patch("api.leads.routes.classify_lead_email"),
    ):
        mock_sync_lead.delay.side_effect = RuntimeError("broker unavailable")

        with caplog.at_level(logging.DEBUG):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                visitor_token = _create_visitor_token()
                body = {
                    "name": "Jane",
                    "email": "jane@example.com",
                    "consent": {
                        "granted": True,
                        "purpose": "contact",
                        "text": "OK",
                    },
                }

                await client.post(
                    "/public/leads",
                    json=body,
                    headers={"Authorization": f"Bearer {visitor_token}"},
                )

    assert "crm_enqueue_failed" in caplog.text


# ---------------------------------------------------------------------------
# SR-21: lead_captured feed emit (D2/D3) -- one of the 3 mandatory call sites
# ---------------------------------------------------------------------------


async def test_post_leads_emits_lead_captured() -> None:
    """After a successful capture, the feed emits exactly one lead_captured
    event with the trusted lead_id (never PII in the payload)."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-emit-1")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch("api.leads.routes.emit_event_safe") as mock_emit,
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        mock_emit.return_value = "event-1"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Jane",
                "email": "jane@example.com",
                "consent": {"granted": True, "purpose": "contact", "text": "OK"},
            }
            response = await client.post(
                "/public/leads", json=body, headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "lead_captured"
    assert kwargs["category"] == "leads"
    assert kwargs["target_id"] == "lead-emit-1"
    assert kwargs["payload"] == {"lead_id": "lead-emit-1"}
    assert "jane@example.com" not in str(kwargs["payload"])


async def test_post_leads_still_201_when_feed_emit_raises() -> None:
    """MANDATORY (D2 -- the most important test in this sprint): if the feed
    insert fails for any reason, the lead capture must still succeed with a
    normal 201 and the lead exists. emit_event_safe itself never raises (it
    is the fail-open boundary), so forcing the underlying events_repository
    call to raise proves the ordering holds all the way through the real
    (non-mocked) emit_event_safe wrapper."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-emit-2")

    with (
        patch("api.leads.routes.create_lead", new=mock_create_lead),
        patch(
            "api.notifications.emit.emit_event",
            new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
        ),
        patch("api.leads.routes.sync_lead"),
        patch("api.leads.routes.classify_lead_email"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Jane",
                "email": "jane@example.com",
                "consent": {"granted": True, "purpose": "contact", "text": "OK"},
            }
            response = await client.post(
                "/public/leads", json=body, headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    data = response.json()
    assert data["lead_id"] == "lead-emit-2"
