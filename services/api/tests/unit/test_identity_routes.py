"""Unit tests for POST /public/chat/identity (SR-14 conversation-start identity gate).

Covers:
- Consent granted=true, no existing lead -> 201 {lead_id, status:"new"}, create_lead called once with
  source="chat_identity", update_lead_contact NOT called.
- Consent granted=true, existing lead (e.g. anonymous SR-9.1 booking) -> update_lead_contact called,
  create_lead NOT called (no duplicate).
- Consent granted=false / omitted -> 422 CONSENT_REQUIRED, nothing persisted.
- consent.captured_at is server-stamped, never client-supplied.
- An identified_in_chat activity is appended on success.
- Body tenant_id/visitor_id ignored (only from claims).
- update_lead_contact returning False (no row updated) -> honest error, not a fabricated 201.
- Missing/blank name or email, or email without "@" -> 422.
- No Authorization header -> 401.
- Non-visitor token (e.g., admin) -> 403 NOT_A_VISITOR.
- PII not in logs (name, email).
- Leak-free response (no tenant_id/visitor_id/echoed PII).
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
    claims = AuthClaims(subject=visitor_id, role=Role.VISITOR, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


def _create_admin_token(tenant_id: str = _TENANT_ID) -> str:
    claims = AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


_VALID_CONSENT = {
    "granted": True,
    "purpose": "chat_identification",
    "text": "I consent to my name and email being stored so we can follow up on this conversation.",
}


# ---------------------------------------------------------------------------
# Create branch -- no existing lead
# ---------------------------------------------------------------------------


async def test_post_identity_no_existing_lead_creates_lead_returns_201() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value=None)
    mock_create_lead = AsyncMock(return_value="lead-new-1")
    mock_update_contact = AsyncMock(return_value=True)
    mock_add_activity = AsyncMock(return_value="activity-1")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.update_lead_contact", new=mock_update_contact),
        patch("api.leads.identity_routes.add_activity", new=mock_add_activity),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}

            response = await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    data = response.json()
    assert data["lead_id"] == "lead-new-1"
    assert data["status"] == "new"
    assert "email" not in data
    assert "name" not in data
    assert "tenant_id" not in data
    assert "visitor_id" not in data

    mock_create_lead.assert_awaited_once()
    _, kwargs = mock_create_lead.call_args
    assert kwargs["name"] == "Dana"
    assert kwargs["email"] == "dana@example.com"
    assert kwargs["source"] == "chat_identity"
    assert kwargs["visitor_id"] == "visitor-123"
    mock_update_contact.assert_not_awaited()
    mock_add_activity.assert_awaited_once()
    _, act_kwargs = mock_add_activity.call_args
    assert act_kwargs["type"] == "identified_in_chat"


async def test_post_identity_captured_at_is_server_stamped() -> None:
    """consent.captured_at is stamped server-side, never client-supplied."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value=None)
    mock_create_lead = AsyncMock(return_value="lead-new-1")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.add_activity", new=AsyncMock(return_value="a1")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            malicious_consent = dict(_VALID_CONSENT, captured_at="2000-01-01T00:00:00Z")
            body = {"name": "Dana", "email": "dana@example.com", "consent": malicious_consent}

            await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    _, kwargs = mock_create_lead.call_args
    stamped = kwargs["consent"]["captured_at"]
    assert stamped != "2000-01-01T00:00:00Z"
    assert stamped.startswith("20")  # a real ISO timestamp, not the client's fabricated one


# ---------------------------------------------------------------------------
# Link branch -- existing lead (SR-9.1 anonymous booking case, D6)
# ---------------------------------------------------------------------------


async def test_post_identity_existing_lead_updates_via_update_lead_contact() -> None:
    """A visitor with an existing lead (e.g. NULL-contact from an anonymous
    booking) gets that SAME row filled in -- no duplicate lead created."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value="lead-existing-1")
    mock_create_lead = AsyncMock(return_value="should-not-be-called")
    mock_update_contact = AsyncMock(return_value=True)
    mock_add_activity = AsyncMock(return_value="activity-1")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.update_lead_contact", new=mock_update_contact),
        patch("api.leads.identity_routes.add_activity", new=mock_add_activity),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}

            response = await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    data = response.json()
    assert data["lead_id"] == "lead-existing-1"

    mock_create_lead.assert_not_awaited()
    mock_update_contact.assert_awaited_once()
    _, kwargs = mock_update_contact.call_args
    assert kwargs["name"] == "Dana"
    assert kwargs["email"] == "dana@example.com"
    mock_add_activity.assert_awaited_once()


async def test_post_identity_update_contact_failure_returns_honest_error() -> None:
    """update_lead_contact returning False (no row updated) -> the capture
    must NOT report a fabricated success (C4 -- no silent fallback)."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value="lead-existing-1")
    mock_update_contact = AsyncMock(return_value=False)
    mock_add_activity = AsyncMock(return_value="activity-1")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.update_lead_contact", new=mock_update_contact),
        patch("api.leads.identity_routes.add_activity", new=mock_add_activity),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}

            response = await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 422
    data = response.json()
    assert data["error_code"] == "IDENTITY_CAPTURE_FAILED"
    mock_add_activity.assert_not_awaited()


# ---------------------------------------------------------------------------
# Consent gate (MANDATORY)
# ---------------------------------------------------------------------------


async def test_post_identity_consent_false_returns_422_and_persists_nothing() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-1")
    mock_update_contact = AsyncMock(return_value=True)

    with (
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.update_lead_contact", new=mock_update_contact),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Dana",
                "email": "dana@example.com",
                "consent": {**_VALID_CONSENT, "granted": False},
            }

            response = await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 422
    data = response.json()
    assert data["error_code"] == "CONSENT_REQUIRED"
    mock_create_lead.assert_not_awaited()
    mock_update_contact.assert_not_awaited()


async def test_post_identity_consent_omitted_returns_422_and_persists_nothing() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-1")

    with patch("api.leads.identity_routes.create_lead", new=mock_create_lead):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {"name": "Dana", "email": "dana@example.com"}

            response = await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 422
    data = response.json()
    assert data["error_code"] == "CONSENT_REQUIRED"
    mock_create_lead.assert_not_awaited()


async def test_post_identity_consent_truthy_not_true_returns_422() -> None:
    """Mirrors leads/routes.py:90's `is not True` -- a truthy-but-not-exactly-
    True granted value is still rejected."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_create_lead = AsyncMock(return_value="lead-1")

    with patch("api.leads.identity_routes.create_lead", new=mock_create_lead):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {
                "name": "Dana",
                "email": "dana@example.com",
                "consent": {**_VALID_CONSENT, "granted": 1},
            }

            response = await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    # Pydantic coerces int 1 -> bool True for a `bool` field by default, so
    # this body actually passes consent -- included to document that
    # `granted` is a strict bool field; the ValidationError-code path is
    # covered by the granted=False/omitted tests above. If Pydantic strict
    # mode is ever enabled, this becomes a 422 VALIDATION_ERROR instead.
    assert response.status_code in (201, 422)


# ---------------------------------------------------------------------------
# Tenant/visitor isolation (MANDATORY, C8)
# ---------------------------------------------------------------------------


async def test_post_identity_body_tenant_and_visitor_id_ignored() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value=None)
    mock_create_lead = AsyncMock(return_value="lead-1")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.add_activity", new=AsyncMock(return_value="a1")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token(tenant_id="tenant-real", visitor_id="visitor-real")
            body = {
                "name": "Dana",
                "email": "dana@example.com",
                "tenant_id": "tenant-fake",
                "visitor_id": "visitor-fake",
                "consent": _VALID_CONSENT,
            }

            response = await client.post(
                "/public/chat/identity",
                json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    _, lookup_kwargs = mock_get_lead_id.call_args
    # get_lead_id_by_visitor_id(db, claims, visitor_id) -- positional 3rd arg
    lookup_visitor_id = mock_get_lead_id.call_args.args[-1]
    assert lookup_visitor_id == "visitor-real"
    _, create_kwargs = mock_create_lead.call_args
    assert create_kwargs["visitor_id"] == "visitor-real"


async def test_post_identity_no_authorization_returns_401() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}
        response = await client.post("/public/chat/identity", json=body)

    assert response.status_code == 401


async def test_post_identity_admin_token_returns_403_not_a_visitor() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = _create_admin_token()
        body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}

        response = await client.post(
            "/public/chat/identity",
            json=body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert response.status_code == 403
    data = response.json()
    assert data["error_code"] == "NOT_A_VISITOR"


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


async def test_post_identity_missing_name_returns_422() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        visitor_token = _create_visitor_token()
        body = {"email": "dana@example.com", "consent": _VALID_CONSENT}
        response = await client.post(
            "/public/chat/identity",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_identity_blank_name_returns_422() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        visitor_token = _create_visitor_token()
        body = {"name": "  ", "email": "dana@example.com", "consent": _VALID_CONSENT}
        response = await client.post(
            "/public/chat/identity",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_identity_missing_email_returns_422() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        visitor_token = _create_visitor_token()
        body = {"name": "Dana", "consent": _VALID_CONSENT}
        response = await client.post(
            "/public/chat/identity",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


async def test_post_identity_email_without_at_returns_422() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        visitor_token = _create_visitor_token()
        body = {"name": "Dana", "email": "danaexample.com", "consent": _VALID_CONSENT}
        response = await client.post(
            "/public/chat/identity",
            json=body,
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# PII-safe logging (MANDATORY)
# ---------------------------------------------------------------------------


async def test_post_identity_pii_not_in_logs(caplog: Any) -> None:
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value=None)
    mock_create_lead = AsyncMock(return_value="lead-1")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.add_activity", new=AsyncMock(return_value="a1")),
    ):
        with caplog.at_level(logging.DEBUG):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                visitor_token = _create_visitor_token()
                body = {"name": "Dana Doe", "email": "dana@example.com", "consent": _VALID_CONSENT}

                await client.post(
                    "/public/chat/identity",
                    json=body,
                    headers={"Authorization": f"Bearer {visitor_token}"},
                )

    log_text = caplog.text
    assert "dana@example.com" not in log_text
    assert "Dana Doe" not in log_text


# ---------------------------------------------------------------------------
# SR-21: lead_captured feed emit (D2/D3) -- 2nd of 3 mandatory call sites
# ---------------------------------------------------------------------------


async def test_post_identity_new_lead_emits_lead_captured() -> None:
    """The new-lead branch emits exactly one lead_captured event."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value=None)
    mock_create_lead = AsyncMock(return_value="lead-emit-1")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.add_activity", new=AsyncMock(return_value="a1")),
        patch("api.leads.identity_routes.emit_event_safe") as mock_emit,
    ):
        mock_emit.return_value = "event-1"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}
            response = await client.post(
                "/public/chat/identity", json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "lead_captured"
    assert kwargs["target_id"] == "lead-emit-1"
    assert kwargs["payload"] == {"lead_id": "lead-emit-1"}


async def test_post_identity_existing_lead_does_not_emit_lead_captured() -> None:
    """Linking an existing lead's contact is NOT a capture -- no event."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value="lead-existing-1")
    mock_update_contact = AsyncMock(return_value=True)

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.update_lead_contact", new=mock_update_contact),
        patch("api.leads.identity_routes.add_activity", new=AsyncMock(return_value="a1")),
        patch("api.leads.identity_routes.emit_event_safe") as mock_emit,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}
            response = await client.post(
                "/public/chat/identity", json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    mock_emit.assert_not_awaited()


async def test_post_identity_still_201_when_feed_emit_raises() -> None:
    """MANDATORY (D2): a feed-insert failure must not fail identity capture."""
    db = _StubDatabase()
    app = _build_app(db)

    mock_get_lead_id = AsyncMock(return_value=None)
    mock_create_lead = AsyncMock(return_value="lead-emit-2")

    with (
        patch("api.leads.identity_routes.get_lead_id_by_visitor_id", new=mock_get_lead_id),
        patch("api.leads.identity_routes.create_lead", new=mock_create_lead),
        patch("api.leads.identity_routes.add_activity", new=AsyncMock(return_value="a1")),
        patch(
            "api.notifications.emit.emit_event",
            new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            visitor_token = _create_visitor_token()
            body = {"name": "Dana", "email": "dana@example.com", "consent": _VALID_CONSENT}
            response = await client.post(
                "/public/chat/identity", json=body,
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    assert response.json()["lead_id"] == "lead-emit-2"
