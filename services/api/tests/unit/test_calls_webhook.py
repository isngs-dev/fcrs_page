"""Unit tests for POST /public/calls/twilio/{tenant_id} (missed-call text-back).

SECURITY-CRITICAL suite mirroring test_calendly_webhook.py's structure.
Covers:
- Valid signature + missed CallStatus -> 200, SMS job enqueued + dispatched.
- Invalid/tampered/missing signature -> 401 TWILIO_SIGNATURE_INVALID, nothing enqueued.
- Cross-tenant rejection (MANDATORY).
- Unknown tenant / no call config / no SMS config -> rejected, no secret leaked.
- Non-missed CallStatus (completed/ringing/in-progress) -> 200 no-op, nothing enqueued.
- Wrong `To` number (not the monitored number) -> 200 no-op, nothing enqueued.
- Feature disabled (`enabled=false`) -> 200 no-op, nothing enqueued.
- Idempotent re-delivery (MANDATORY): same CallSid twice -> exactly ONE job.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

from common.cache import InMemoryCache
from common.crypto import SecretBox
from httpx import ASGITransport, AsyncClient

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"
_OTHER_TENANT_ID = "tenant-xyz-999"
_AUTH_TOKEN = "twilio" + "-" + "auth" + "-" + "token"  # not a real credential
_OTHER_AUTH_TOKEN = "other" + "-" + "tenant" + "-" + "token"
_WRONG_AUTH_TOKEN = "wrong" + "-" + "auth" + "-" + "value"
_MONITORED_NUMBER = "+15005550006"
_TEXT_BACK_MESSAGE = "Sorry we missed your call! Reply here or visit example.com/chat."

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


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


class _StubDatabase:
    """In-memory stub backing the webhook receiver's downstream repos."""

    def __init__(self) -> None:
        self._call_configs: dict[str, dict[str, Any]] = {}
        self._sms_configs: dict[str, dict[str, Any]] = {}
        self._jobs: dict[tuple[str, str], dict[str, Any]] = {}

    def seed_call_config(
        self, tenant_id: str, *, monitored_number: str = _MONITORED_NUMBER,
        enabled: bool = True, message: str = _TEXT_BACK_MESSAGE,
    ) -> None:
        self._call_configs[tenant_id] = {
            "monitored_phone_number": monitored_number,
            "enabled": enabled,
            "text_back_message": message,
        }

    def seed_sms_config(self, tenant_id: str, *, auth_token: str, provider: str = "twilio") -> None:
        with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
            _reset_settings()
            from api.config import get_api_settings

            box = SecretBox(get_api_settings().secret_encryption_key)
        self._sms_configs[tenant_id] = {
            "provider": provider,
            "from_address": None,
            "from_name": None,
            "smtp_host": None,
            "smtp_port": None,
            "smtp_use_tls": False,
            "smtp_username": None,
            "twilio_account_sid": "ACxxxx",
            "twilio_from": "+15005550001",
            "credentials_ciphertext": box.encrypt(auth_token) if auth_token else None,
            "enabled": True,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANT_CALL_CONFIGS" in q:
            return self._call_configs.get(args[0])
        if "FROM TENANT_NOTIFICATION_CONFIGS" in q:
            tenant_id, channel = args
            row = self._sms_configs.get(tenant_id)
            return row if row is not None and channel == "sms" else None
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = query.strip().upper()
        if q.startswith("INSERT INTO NOTIFICATION_JOBS"):
            (job_id, tenant_id, channel, template, recipient, subject, body, payload,
             dedupe_key, lead_id) = args
            key = (tenant_id, dedupe_key)
            if key in self._jobs:
                return None
            self._jobs[key] = {
                "job_id": job_id, "tenant_id": tenant_id, "channel": channel,
                "recipient": recipient, "body": body, "dedupe_key": dedupe_key,
            }
            return job_id
        return None

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
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


def _build_app(db: _StubDatabase) -> Any:
    _reset_settings()
    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()
    app.state.db = db
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    return app


def _sign(auth_token: str, url: str, params: dict[str, str]) -> str:
    signed = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _call_status_params(
    *, call_sid: str = "CAxxxx1", call_status: str = "no-answer",
    from_number: str = "+15558675309", to_number: str = _MONITORED_NUMBER,
) -> dict[str, str]:
    return {"CallSid": call_sid, "CallStatus": call_status, "From": from_number, "To": to_number}


async def _post_call_status(
    app: Any, tenant_id: str, params: dict[str, str], *, auth_token: str | None = None,
) -> Any:
    url = f"http://test/public/calls/twilio/{tenant_id}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if auth_token is not None:
        headers["X-Twilio-Signature"] = _sign(auth_token, url, params)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/public/calls/twilio/{tenant_id}", content=urlencode(params), headers=headers,
        )


# ---------------------------------------------------------------------------
# Valid signature + missed call -> 200, SMS enqueued + dispatched
# ---------------------------------------------------------------------------


async def test_missed_call_enqueues_and_dispatches_textback() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    with patch("api.calls.webhook.send_notification.delay") as mock_delay:
        response = await _post_call_status(
            app, _TENANT_ID, _call_status_params(), auth_token=_AUTH_TOKEN,
        )

    assert response.status_code == 200
    assert len(db._jobs) == 1
    job = next(iter(db._jobs.values()))
    assert job["channel"] == "sms"
    assert job["recipient"] == "+15558675309"
    assert job["body"] == _TEXT_BACK_MESSAGE
    mock_delay.assert_called_once()


# ---------------------------------------------------------------------------
# Invalid / tampered / missing signature -> 401, nothing enqueued
# ---------------------------------------------------------------------------


async def test_missing_signature_header_returns_401_nothing_enqueued() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    response = await _post_call_status(app, _TENANT_ID, _call_status_params(), auth_token=None)

    assert response.status_code == 401
    assert response.json()["error_code"] == "TWILIO_SIGNATURE_INVALID"
    assert db._jobs == {}


async def test_wrong_auth_token_returns_401_nothing_enqueued() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(), auth_token=_WRONG_AUTH_TOKEN,
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "TWILIO_SIGNATURE_INVALID"
    assert db._jobs == {}


async def test_tampered_param_returns_401_nothing_enqueued() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    url = f"http://test/public/calls/twilio/{_TENANT_ID}"
    signed_params = _call_status_params()
    signature = _sign(_AUTH_TOKEN, url, signed_params)
    tampered_params = _call_status_params(from_number="+19995551234")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/public/calls/twilio/{_TENANT_ID}",
            content=urlencode(tampered_params),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signature,
            },
        )

    assert response.status_code == 401
    assert db._jobs == {}


# ---------------------------------------------------------------------------
# Cross-tenant rejection (MANDATORY)
# ---------------------------------------------------------------------------


async def test_cross_tenant_signature_rejected_nothing_enqueued_either_tenant() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    db.seed_call_config(_OTHER_TENANT_ID)
    db.seed_sms_config(_OTHER_TENANT_ID, auth_token=_OTHER_AUTH_TOKEN)
    app = _build_app(db)

    # Sign for tenant A's URL/token, but POST to tenant B's path.
    params = _call_status_params()
    url_for_a = f"http://test/public/calls/twilio/{_TENANT_ID}"
    signature_for_a = _sign(_AUTH_TOKEN, url_for_a, params)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/public/calls/twilio/{_OTHER_TENANT_ID}",
            content=urlencode(params),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signature_for_a,
            },
        )

    assert response.status_code == 401
    assert db._jobs == {}


# ---------------------------------------------------------------------------
# Unknown tenant / no call config / no SMS config -> rejected, no secret leaked
# ---------------------------------------------------------------------------


async def test_unknown_tenant_rejected_no_secret_leaked() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    response = await _post_call_status(
        app, "unknown-tenant", _call_status_params(), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 401
    assert _AUTH_TOKEN not in response.text
    assert db._jobs == {}


async def test_call_config_never_set_rejected() -> None:
    db = _StubDatabase()
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 401
    assert db._jobs == {}


async def test_sms_never_configured_rejected() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 401
    assert db._jobs == {}


async def test_non_twilio_sms_provider_rejected() -> None:
    """The webhook's own verification only makes sense against a Twilio Auth
    Token -- a tenant configured for a different SMS provider is rejected."""
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN, provider="other-sms-provider")
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 401
    assert db._jobs == {}


# ---------------------------------------------------------------------------
# Non-missed CallStatus -> 200 no-op
# ---------------------------------------------------------------------------


async def test_completed_call_status_is_a_noop() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(call_status="completed"), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 200
    assert db._jobs == {}


async def test_ringing_call_status_is_a_noop() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(call_status="ringing"), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 200
    assert db._jobs == {}


# ---------------------------------------------------------------------------
# Wrong `To` number -> 200 no-op
# ---------------------------------------------------------------------------


async def test_call_to_unmonitored_number_is_a_noop() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID, monitored_number="+15005550006")
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(to_number="+15005559999"), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 200
    assert db._jobs == {}


# ---------------------------------------------------------------------------
# Feature disabled -> 200 no-op
# ---------------------------------------------------------------------------


async def test_disabled_config_is_a_noop() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID, enabled=False)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    response = await _post_call_status(
        app, _TENANT_ID, _call_status_params(), auth_token=_AUTH_TOKEN,
    )

    assert response.status_code == 200
    assert db._jobs == {}


# ---------------------------------------------------------------------------
# Idempotent re-delivery (MANDATORY)
# ---------------------------------------------------------------------------


async def test_idempotent_redelivery_exactly_one_job() -> None:
    db = _StubDatabase()
    db.seed_call_config(_TENANT_ID)
    db.seed_sms_config(_TENANT_ID, auth_token=_AUTH_TOKEN)
    app = _build_app(db)

    with patch("api.calls.webhook.send_notification.delay"):
        first = await _post_call_status(
            app, _TENANT_ID, _call_status_params(), auth_token=_AUTH_TOKEN,
        )
        # A second delivery of the SAME CallSid (Twilio retry, or a second
        # terminal status for the same call) must never enqueue a second job.
        second = await _post_call_status(
            app, _TENANT_ID, _call_status_params(call_status="busy"), auth_token=_AUTH_TOKEN,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(db._jobs) == 1
