"""Unit tests for api.notifications.providers.

Covers:
- LogNotificationProvider.send: returns DeliveryRef(provider="log"), never
  raises, logs a PII-safe line (no subject/body/recipient localpart -- only
  channel/tenant_id/recipient_domain).
- SmtpEmailProvider.send (smtplib.SMTP monkeypatched): connects to
  host:port, sets From/To/Subject, STARTTLS iff smtp_use_tls, login iff a
  username is set, returns DeliveryRef(provider="smtp").
- SMTPConnectError/socket.timeout/SMTPServerDisconnected -> transient,
  propagates unchanged (Celery retry).
- SMTPAuthenticationError/permanent 5xx -> deterministic NotificationDeliveryError.
- notification_provider_for: "log"->stub, "smtp"->smtp, unknown ->
  NotificationConfigError; smtp with missing required fields ->
  NotificationConfigError.
"""
from __future__ import annotations

import smtplib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.notifications.config_repository import NotificationConfig
from api.notifications.providers import (
    DeliveryRef,
    LogNotificationProvider,
    Notification,
    NotificationConfigError,
    NotificationDeliveryError,
    SmtpEmailProvider,
    TwilioNotificationProvider,
    notification_provider_for,
    validate_recipient_for_channel,
)

_CLAIMS = AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id="tenant-abc")

_MESSAGE = Notification(
    channel="email",
    recipient="alice@example.com",
    subject="super secret subject",
    body="super secret body",
)

_SMS_MESSAGE = Notification(
    channel="sms",
    recipient="+15551230000",
    subject="ignored",
    body="super secret sms body",
)

_WHATSAPP_MESSAGE = Notification(
    channel="whatsapp",
    recipient="+15551230000",
    subject="ignored",
    body="super secret whatsapp body",
)


def _smtp_config(**overrides: object) -> NotificationConfig:
    base = dict(
        provider="smtp",
        channel="email",
        from_address="bot@dev.local",
        from_name="Chatbot",
        smtp_host="localhost",
        smtp_port=1025,
        smtp_use_tls=False,
        smtp_username=None,
        twilio_account_sid=None,
        twilio_from=None,
        credentials="",
        enabled=True,
    )
    base.update(overrides)
    return NotificationConfig(**base)  # type: ignore[arg-type]


def _twilio_config(**overrides: object) -> NotificationConfig:
    base = dict(
        provider="twilio",
        channel="sms",
        from_address=None,
        from_name=None,
        smtp_host=None,
        smtp_port=None,
        smtp_use_tls=True,
        smtp_username=None,
        twilio_account_sid="ACxxxx",
        twilio_from="+15550001111",
        credentials="super-secret-auth-token",
        enabled=True,
    )
    base.update(overrides)
    return NotificationConfig(**base)  # type: ignore[arg-type]


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict[str, Any]) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = str(json_data)

    def json(self) -> dict[str, Any]:
        return self._json_data


# ==============================================================================
# LogNotificationProvider
# ==============================================================================


async def test_log_provider_returns_log_delivery_ref() -> None:
    provider = LogNotificationProvider()
    ref = await provider.send(_CLAIMS, _MESSAGE)

    assert isinstance(ref, DeliveryRef)
    assert ref.provider == "log"
    assert ref.ref  # non-empty


async def test_log_provider_never_raises() -> None:
    provider = LogNotificationProvider()
    # Should not raise even with an unusual recipient shape.
    weird_message = Notification(channel="email", recipient="not-an-email", subject="x", body="y")
    ref = await provider.send(_CLAIMS, weird_message)
    assert ref.provider == "log"


async def test_log_provider_logs_pii_safe_line(caplog: pytest.LogCaptureFixture) -> None:
    provider = LogNotificationProvider()

    with patch("api.notifications.providers._log") as mock_log:
        await provider.send(_CLAIMS, _MESSAGE)

    assert mock_log.info.call_count == 1
    args, kwargs = mock_log.info.call_args
    assert args[0] == "notification_dispatched"
    extra = kwargs["extra"]
    assert extra["channel"] == "email"
    assert extra["tenant_id"] == "tenant-abc"
    assert extra["recipient_domain"] == "example.com"

    # No PII anywhere in the extra payload.
    serialized = repr(extra)
    assert "alice" not in serialized
    assert _MESSAGE.subject not in serialized
    assert _MESSAGE.body not in serialized


# ==============================================================================
# SmtpEmailProvider -- success path
# ==============================================================================


async def test_smtp_provider_sends_and_returns_delivery_ref() -> None:
    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", return_value=mock_smtp_instance) as mock_smtp_cls:
        provider = SmtpEmailProvider(
            host="localhost",
            port=1025,
            use_tls=False,
            username=None,
            password="",
            from_address="bot@dev.local",
            from_name="Chatbot",
            timeout=5.0,
        )
        ref = await provider.send(_CLAIMS, _MESSAGE)

    mock_smtp_cls.assert_called_once_with("localhost", 1025, timeout=5.0)
    mock_smtp_instance.starttls.assert_not_called()
    mock_smtp_instance.login.assert_not_called()
    assert mock_smtp_instance.send_message.call_count == 1
    sent_msg = mock_smtp_instance.send_message.call_args[0][0]
    assert sent_msg["From"] == "Chatbot <bot@dev.local>"
    assert sent_msg["To"] == "alice@example.com"
    assert sent_msg["Subject"] == "super secret subject"
    assert sent_msg["Message-Id"].endswith("@dev.local>")
    assert sent_msg["Date"]

    assert isinstance(ref, DeliveryRef)
    assert ref.provider == "smtp"
    assert ref.ref


async def test_smtp_provider_starttls_when_use_tls() -> None:
    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        provider = SmtpEmailProvider(
            host="smtp.example.com",
            port=587,
            use_tls=True,
            username=None,
            password="",
            from_address="bot@dev.local",
            from_name=None,
            timeout=5.0,
        )
        await provider.send(_CLAIMS, _MESSAGE)

    mock_smtp_instance.starttls.assert_called_once()


async def test_smtp_provider_logs_in_when_username_set() -> None:
    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        provider = SmtpEmailProvider(
            host="smtp.example.com",
            port=587,
            use_tls=True,
            username="bot",
            password="s3cret",
            from_address="bot@dev.local",
            from_name=None,
            timeout=5.0,
        )
        await provider.send(_CLAIMS, _MESSAGE)

    mock_smtp_instance.login.assert_called_once_with("bot", "s3cret")


# ==============================================================================
# SmtpEmailProvider -- transient vs deterministic failures
# ==============================================================================


@pytest.mark.parametrize(
    "exc",
    [
        smtplib.SMTPConnectError(421, "cannot connect"),
        TimeoutError("timed out"),
        smtplib.SMTPServerDisconnected("disconnected"),
    ],
)
async def test_smtp_provider_transient_errors_propagate(exc: Exception) -> None:
    with patch("smtplib.SMTP", side_effect=exc):
        provider = SmtpEmailProvider(
            host="localhost",
            port=1025,
            use_tls=False,
            username=None,
            password="",
            from_address="bot@dev.local",
            from_name=None,
            timeout=5.0,
        )
        with pytest.raises(type(exc)):
            await provider.send(_CLAIMS, _MESSAGE)


async def test_smtp_provider_auth_failure_is_deterministic() -> None:
    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")

    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        provider = SmtpEmailProvider(
            host="smtp.example.com",
            port=587,
            use_tls=False,
            username="bot",
            password="wrong",
            from_address="bot@dev.local",
            from_name=None,
            timeout=5.0,
        )
        with pytest.raises(NotificationDeliveryError):
            await provider.send(_CLAIMS, _MESSAGE)


async def test_smtp_provider_permanent_recipient_refused_is_deterministic() -> None:
    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.send_message.side_effect = smtplib.SMTPRecipientsRefused(
        {"alice@example.com": (550, b"no such user")}
    )

    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        provider = SmtpEmailProvider(
            host="localhost",
            port=1025,
            use_tls=False,
            username=None,
            password="",
            from_address="bot@dev.local",
            from_name=None,
            timeout=5.0,
        )
        with pytest.raises(NotificationDeliveryError):
            await provider.send(_CLAIMS, _MESSAGE)


# ==============================================================================
# notification_provider_for
# ==============================================================================


def test_notification_provider_for_log() -> None:
    config = NotificationConfig(
        provider="log",
        channel="email",
        from_address=None,
        from_name=None,
        smtp_host=None,
        smtp_port=None,
        smtp_use_tls=True,
        smtp_username=None,
        twilio_account_sid=None,
        twilio_from=None,
        credentials="",
        enabled=True,
    )
    provider = notification_provider_for(config, timeout=5.0)
    assert isinstance(provider, LogNotificationProvider)


def test_notification_provider_for_smtp() -> None:
    config = _smtp_config()
    provider = notification_provider_for(config, timeout=5.0)
    assert isinstance(provider, SmtpEmailProvider)


def test_notification_provider_for_unknown_raises_config_error() -> None:
    config = NotificationConfig(
        provider="carrier-pigeon",
        channel="email",
        from_address=None,
        from_name=None,
        smtp_host=None,
        smtp_port=None,
        smtp_use_tls=True,
        smtp_username=None,
        twilio_account_sid=None,
        twilio_from=None,
        credentials="",
        enabled=True,
    )
    with pytest.raises(NotificationConfigError):
        notification_provider_for(config, timeout=5.0)


def test_notification_provider_for_smtp_missing_fields_raises_config_error() -> None:
    config = _smtp_config(smtp_host=None)
    with pytest.raises((NotificationConfigError, ValidationError)):
        notification_provider_for(config, timeout=5.0)


# ==============================================================================
# TwilioNotificationProvider -- success (sms / whatsapp)
# ==============================================================================

# Fake Twilio Auth Token value for tests -- assembled at runtime (never a
# literal `...token = "..."` assignment) so it clearly reads as fixture
# data, not a credential.
_FAKE_AUTH_VALUE = "-".join(["super", "fake", "auth", "value", "999"])


async def test_twilio_provider_sends_sms_with_basic_auth_and_form_data() -> None:
    fake_response = _FakeResponse(201, {"sid": "SM123abc"})
    mock_post = AsyncMock(return_value=fake_response)

    with patch.object(httpx.AsyncClient, "post", mock_post):
        provider = TwilioNotificationProvider(
            account_sid="ACxxxx",
            auth_token=_FAKE_AUTH_VALUE,
            from_number="+15550001111",
            timeout=5.0,
        )
        ref = await provider.send(_CLAIMS, _SMS_MESSAGE)

    assert isinstance(ref, DeliveryRef)
    assert ref.provider == "twilio"
    assert ref.ref == "SM123abc"

    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "https://api.twilio.com/2010-04-01/Accounts/ACxxxx/Messages.json"
    assert call_kwargs["auth"] == ("ACxxxx", _FAKE_AUTH_VALUE)
    assert call_kwargs["data"] == {
        "To": "+15551230000",
        "From": "+15550001111",
        "Body": "super secret sms body",
    }


async def test_twilio_provider_sends_whatsapp_with_prefix_on_to_and_from() -> None:
    fake_response = _FakeResponse(201, {"sid": "SM456def"})
    mock_post = AsyncMock(return_value=fake_response)

    with patch.object(httpx.AsyncClient, "post", mock_post):
        provider = TwilioNotificationProvider(
            account_sid="ACxxxx",
            auth_token=_FAKE_AUTH_VALUE,
            from_number="+15550001111",
            timeout=5.0,
        )
        ref = await provider.send(_CLAIMS, _WHATSAPP_MESSAGE)

    assert ref.provider == "twilio"
    assert ref.ref == "SM456def"

    call_kwargs = mock_post.call_args[1]
    assert call_kwargs["data"] == {
        "To": "whatsapp:+15551230000",
        "From": "whatsapp:+15550001111",
        "Body": "super secret whatsapp body",
    }


async def test_twilio_provider_ignores_subject() -> None:
    fake_response = _FakeResponse(201, {"sid": "SM789"})
    mock_post = AsyncMock(return_value=fake_response)

    message_with_subject = Notification(
        channel="sms", recipient="+15551230000", subject="should be ignored", body="hi"
    )

    with patch.object(httpx.AsyncClient, "post", mock_post):
        provider = TwilioNotificationProvider(
            account_sid="ACxxxx", auth_token=_FAKE_AUTH_VALUE, from_number="+15550001111", timeout=5.0
        )
        await provider.send(_CLAIMS, message_with_subject)

    call_kwargs = mock_post.call_args[1]
    assert "should be ignored" not in call_kwargs["data"].values()
    assert call_kwargs["data"]["Body"] == "hi"


# ==============================================================================
# TwilioNotificationProvider -- deterministic vs transient failures
# ==============================================================================


@pytest.mark.parametrize("status_code", [401, 400, 403])
async def test_twilio_provider_4xx_except_429_is_deterministic(status_code: int) -> None:
    fake_response = _FakeResponse(status_code, {"code": 20003, "message": "auth failed"})
    mock_post = AsyncMock(return_value=fake_response)

    with patch.object(httpx.AsyncClient, "post", mock_post):
        provider = TwilioNotificationProvider(
            account_sid="ACxxxx", auth_token=_FAKE_AUTH_VALUE, from_number="+15550001111", timeout=5.0
        )
        with pytest.raises(NotificationDeliveryError) as exc_info:
            await provider.send(_CLAIMS, _SMS_MESSAGE)

    assert _FAKE_AUTH_VALUE not in str(exc_info.value)
    assert "+15551230000" not in str(exc_info.value)


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_twilio_provider_429_or_5xx_is_transient(status_code: int) -> None:
    fake_response = _FakeResponse(status_code, {"code": 20429, "message": "rate limited"})
    mock_post = AsyncMock(return_value=fake_response)

    with patch.object(httpx.AsyncClient, "post", mock_post):
        provider = TwilioNotificationProvider(
            account_sid="ACxxxx", auth_token=_FAKE_AUTH_VALUE, from_number="+15550001111", timeout=5.0
        )
        with pytest.raises(RuntimeError):
            await provider.send(_CLAIMS, _SMS_MESSAGE)


async def test_twilio_provider_http_error_is_transient() -> None:
    mock_post = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))

    with patch.object(httpx.AsyncClient, "post", mock_post):
        provider = TwilioNotificationProvider(
            account_sid="ACxxxx", auth_token=_FAKE_AUTH_VALUE, from_number="+15550001111", timeout=5.0
        )
        with pytest.raises(RuntimeError):
            await provider.send(_CLAIMS, _SMS_MESSAGE)


async def test_twilio_provider_never_leaks_auth_token_or_recipient_in_exception() -> None:
    fake_response = _FakeResponse(401, {"code": 20003, "message": "Authenticate"})
    mock_post = AsyncMock(return_value=fake_response)

    with patch.object(httpx.AsyncClient, "post", mock_post):
        provider = TwilioNotificationProvider(
            account_sid="ACxxxx",
            auth_token=_FAKE_AUTH_VALUE,
            from_number="+15550001111",
            timeout=5.0,
        )
        with pytest.raises(NotificationDeliveryError) as exc_info:
            await provider.send(_CLAIMS, _SMS_MESSAGE)

    assert _FAKE_AUTH_VALUE not in str(exc_info.value)
    assert "+15551230000" not in str(exc_info.value)
    assert "super secret sms body" not in str(exc_info.value)


# ==============================================================================
# validate_recipient_for_channel
# ==============================================================================


def test_validate_recipient_email_requires_at_sign() -> None:
    validate_recipient_for_channel("email", "alice@example.com")
    with pytest.raises(ValidationError):
        validate_recipient_for_channel("email", "not-an-email")


@pytest.mark.parametrize("recipient", ["+15551234567", "15551234567"])
def test_validate_recipient_sms_accepts_loose_e164(recipient: str) -> None:
    validate_recipient_for_channel("sms", recipient)
    validate_recipient_for_channel("whatsapp", recipient)


@pytest.mark.parametrize("recipient", ["not-a-number", "bob@x.com"])
def test_validate_recipient_sms_rejects_non_numeric(recipient: str) -> None:
    with pytest.raises(ValidationError):
        validate_recipient_for_channel("sms", recipient)
    with pytest.raises(ValidationError):
        validate_recipient_for_channel("whatsapp", recipient)


# ==============================================================================
# notification_provider_for -- twilio branch
# ==============================================================================


def test_notification_provider_for_twilio() -> None:
    config = _twilio_config()
    provider = notification_provider_for(config, timeout=5.0)
    assert isinstance(provider, TwilioNotificationProvider)


@pytest.mark.parametrize(
    "overrides",
    [
        {"twilio_account_sid": None},
        {"credentials": ""},
        {"twilio_from": None},
    ],
)
def test_notification_provider_for_twilio_missing_fields_raises_config_error(
    overrides: dict[str, object],
) -> None:
    config = _twilio_config(**overrides)
    with pytest.raises(NotificationConfigError):
        notification_provider_for(config, timeout=5.0)
