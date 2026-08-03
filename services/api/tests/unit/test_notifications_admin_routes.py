"""Unit tests for POST /admin/notifications/test-send.

Covers:
- CLIENT_ADMIN only (agent/visitor -> 403, no auth -> 401).
- A new enqueue -> 202 + send_notification.delay called once (mock .delay).
- Re-posting the SAME explicit dedupe_key -> deduplicated: true and .delay
  NOT called again (idempotent enqueue proven at the route).
- No credentials/PII ever appear in the response.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

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

_BODY = {
    "recipient": "you@dev.local",
    "subject": "S9.1 test",
    "body": "hello from the test suite",
}


class _StubDatabase:
    """In-memory stub database for notification_jobs (test-send route)."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = query.strip().upper()
        if q.startswith("INSERT INTO NOTIFICATION_JOBS"):
            (job_id, tenant_id, channel, template, recipient, subject, body, payload, dedupe_key, lead_id) = args
            for row in self._jobs.values():
                if row["tenant_id"] == tenant_id and row["dedupe_key"] == dedupe_key:
                    return None
            self._jobs[job_id] = {
                "job_id": job_id, "tenant_id": tenant_id, "channel": channel,
                "dedupe_key": dedupe_key, "status": "pending",
            }
            return job_id
        if q.startswith("SELECT JOB_ID FROM NOTIFICATION_JOBS"):
            tenant_id, dedupe_key = args
            for row in self._jobs.values():
                if row["tenant_id"] == tenant_id and row["dedupe_key"] == dedupe_key:
                    return row["job_id"]
            return None
        return None

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return []

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

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    return app


def _token(role: Role, tenant_id: str | None = _TENANT_ID) -> str:
    claims = AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


async def test_client_admin_test_send_returns_202_and_enqueues_delay() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/notifications/test-send", json=_BODY, cookies={"access_token": token}
            )

        assert response.status_code == 202
        data = response.json()
        assert data["deduplicated"] is False
        assert data["job_id"]
        mock_task.delay.assert_called_once()
        call_kwargs = mock_task.delay.call_args.kwargs
        assert call_kwargs["tenant_id"] == _TENANT_ID
        assert call_kwargs["job_id"] == data["job_id"]


async def test_duplicate_dedupe_key_returns_deduplicated_and_does_not_delay_again() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body = {**_BODY, "dedupe_key": "test:fixed-key-1"}

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            first = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )
            second = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )

        assert first.status_code == 202
        assert first.json()["deduplicated"] is False
        assert second.status_code == 202
        assert second.json()["deduplicated"] is True
        assert second.json()["job_id"] == first.json()["job_id"]
        mock_task.delay.assert_called_once()


async def test_client_agent_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/notifications/test-send", json=_BODY, cookies={"access_token": token}
        )

    assert response.status_code == 403


async def test_visitor_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post(
            "/admin/notifications/test-send", json=_BODY, cookies={"access_token": token}
        )

    assert response.status_code == 403


async def test_no_auth_returns_401() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/notifications/test-send", json=_BODY)

    assert response.status_code == 401


async def test_response_never_contains_pii_or_credentials() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/notifications/test-send", json=_BODY, cookies={"access_token": token}
            )

    assert "you@dev.local" not in response.text
    assert _BODY["subject"] not in response.text
    assert _BODY["body"] not in response.text


# ==============================================================================
# Channel-aware test-send (S9.3)
# ==============================================================================


async def test_default_channel_is_email_regression() -> None:
    """S9.1 regression: a body with no ``channel`` still enqueues an email job."""
    db = _StubDatabase()
    app = _build_app(db)

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/notifications/test-send", json=_BODY, cookies={"access_token": token}
            )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert db._jobs[job_id]["channel"] == "email"


async def test_sms_test_send_returns_202_and_enqueues() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body = {"channel": "sms", "recipient": "+15551230000", "body": "hi via sms"}

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )

    assert response.status_code == 202
    data = response.json()
    assert data["deduplicated"] is False
    mock_task.delay.assert_called_once()
    assert db._jobs[data["job_id"]]["channel"] == "sms"


async def test_sms_test_send_dedupe_key_no_double_delay() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body = {
        "channel": "sms",
        "recipient": "+15551230000",
        "body": "hi via sms",
        "dedupe_key": "test:sms-fixed-1",
    }

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            first = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )
            second = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )

    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    assert second.json()["job_id"] == first.json()["job_id"]
    mock_task.delay.assert_called_once()


async def test_whatsapp_test_send_bad_recipient_returns_422_no_enqueue() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body = {"channel": "whatsapp", "recipient": "nope", "body": "hi"}

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )

    assert response.status_code == 422
    mock_task.delay.assert_not_called()
    assert len(db._jobs) == 0


async def test_email_test_send_bad_recipient_returns_422() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body = {"recipient": "not-an-email", "subject": "x", "body": "y"}

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )

    assert response.status_code == 422
    mock_task.delay.assert_not_called()


async def test_sms_test_send_no_subject_required() -> None:
    """SMS/WhatsApp have no subject -- the default empty subject is accepted."""
    db = _StubDatabase()
    app = _build_app(db)

    body = {"channel": "sms", "recipient": "+15551230000", "body": "hi"}

    with patch("api.notifications.admin_routes.send_notification") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                "/admin/notifications/test-send", json=body, cookies={"access_token": token}
            )

    assert response.status_code == 202
