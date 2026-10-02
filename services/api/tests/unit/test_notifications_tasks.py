"""Unit tests for api.notifications.tasks._execute (send_notification's async core).

Covers:
- pending job -> provider.send called + flip to sent + delivery_ref set.
- sent job -> no-op, provider NOT called (redelivery/double-send guard).
- failed job -> no-op.
- no/disabled config -> failed + last_error, NOT raised (no retry).
- unknown provider / missing SMTP fields -> failed, not raised.
- deterministic provider error -> failed, not raised.
- transient provider error -> raises (Celery retry).
- AuthClaims built from the claimed row's own tenant_id (tenant re-scoping).
- PII redaction: no log line carries recipient/subject/body.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.notifications.providers import (
    DeliveryRef,
    NotificationDeliveryError,
)
from api.notifications.tasks import _execute

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_TENANT_A = "tenant-abc"
_TENANT_B = "tenant-xyz"

_PII_RECIPIENT = "alice@example.com"
_PII_SUBJECT = "super secret subject"
_PII_BODY = "super secret body"


class _StubDatabase:
    """In-memory stub database seeded with a single notification_jobs row."""

    def __init__(
        self,
        *,
        job: dict[str, Any] | None,
        config: dict[str, Any] | None,
    ) -> None:
        self._job = job
        self._config = config
        self.update_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.upper()
        if "FROM NOTIFICATION_JOBS" in q:
            tenant_id, job_id = args
            if self._job is None:
                return None
            if self._job["tenant_id"] != tenant_id or self._job["job_id"] != job_id:
                return None
            return self._job
        if "FROM TENANT_NOTIFICATION_CONFIGS" in q:
            tenant_id, channel = args
            if self._config is None or self._config.get("tenant_id") != tenant_id:
                return None
            if self._config.get("channel", "email") != channel:
                return None
            return self._config
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.update_calls.append((query, args))
        q = query.upper()
        if q.startswith("UPDATE NOTIFICATION_JOBS"):
            if "STATUS = 'PENDING'" in q:
                tenant_id, job_id, status, delivery_ref = args
                if self._job is None or self._job["status"] != "pending":
                    return "UPDATE 0"
                self._job["status"] = status
                self._job["delivery_ref"] = delivery_ref
                if "ATTEMPTS + 1" in q:
                    self._job["attempts"] += 1
                return "UPDATE 1"
            tenant_id, job_id, status, last_error = args
            if self._job is not None:
                self._job["status"] = status
                self._job["last_error"] = last_error
                if "ATTEMPTS + 1" in q:
                    self._job["attempts"] += 1
            return "UPDATE 1"
        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return []

    async def close(self) -> None:
        pass


def _job_row(
    *,
    tenant_id: str = _TENANT_A,
    job_id: str = "job-1",
    status: str = "pending",
    channel: str = "email",
) -> dict[str, Any]:
    return {
        "job_id": job_id, "tenant_id": tenant_id, "channel": channel,
        "template": None, "recipient": _PII_RECIPIENT, "subject": _PII_SUBJECT,
        "body": _PII_BODY, "payload": None, "dedupe_key": "test:1",
        "status": status, "attempts": 0, "delivery_ref": None, "last_error": None,
        "created_at": _NOW, "updated_at": _NOW,
    }


def _smtp_config_row(*, tenant_id: str = _TENANT_A, enabled: bool = True) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id, "channel": "email", "provider": "smtp",
        "from_address": "bot@dev.local", "from_name": "Chatbot", "smtp_host": "localhost",
        "smtp_port": 1025, "smtp_use_tls": False, "smtp_username": None,
        "twilio_account_sid": None, "twilio_from": None,
        "credentials_ciphertext": None, "enabled": enabled,
    }


def _log_config_row(*, tenant_id: str = _TENANT_A, enabled: bool = True) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id, "channel": "email", "provider": "log", "from_address": None,
        "from_name": None, "smtp_host": None, "smtp_port": None,
        "smtp_use_tls": True, "smtp_username": None,
        "twilio_account_sid": None, "twilio_from": None,
        "credentials_ciphertext": None, "enabled": enabled,
    }


def _twilio_config_row(
    *, tenant_id: str = _TENANT_A, channel: str = "sms", enabled: bool = True
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id, "channel": channel, "provider": "twilio",
        "from_address": None, "from_name": None, "smtp_host": None, "smtp_port": None,
        "smtp_use_tls": True, "smtp_username": None,
        "twilio_account_sid": "ACxxxx", "twilio_from": "+15550001111",
        "credentials_ciphertext": None, "enabled": enabled,
    }


# ==============================================================================
# Happy path
# ==============================================================================


async def test_pending_job_sends_and_flips_to_sent() -> None:
    job = _job_row(status="pending")
    config = _log_config_row()
    db = _StubDatabase(job=job, config=config)

    fake_provider = AsyncMock()
    fake_provider.send.return_value = DeliveryRef(provider="log", ref="ref-123")

    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ):
        result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "sent"
    assert fake_provider.send.await_count == 1
    assert job["status"] == "sent"
    assert job["delivery_ref"] == "ref-123"
    assert job["attempts"] == 1


# ==============================================================================
# Redelivery / double-send guards
# ==============================================================================


async def test_sent_job_is_no_op_provider_not_called() -> None:
    job = _job_row(status="sent")
    db = _StubDatabase(job=job, config=_log_config_row())

    fake_provider = AsyncMock()
    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ):
        result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "no_op"
    fake_provider.send.assert_not_called()


async def test_failed_job_is_no_op() -> None:
    job = _job_row(status="failed")
    db = _StubDatabase(job=job, config=_log_config_row())

    fake_provider = AsyncMock()
    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ):
        result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "no_op"
    fake_provider.send.assert_not_called()


async def test_missing_job_is_no_op() -> None:
    db = _StubDatabase(job=None, config=_log_config_row())
    result = await _execute(db, job_id="missing", tenant_id=_TENANT_A, smtp_timeout=5.0)
    assert result["status"] == "no_op"


# ==============================================================================
# Deterministic config errors -- failed, NOT raised
# ==============================================================================


async def test_no_config_marks_failed_not_raised() -> None:
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=None)

    result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "failed"
    assert job["status"] == "failed"
    assert job["last_error"] is not None


@pytest.fixture
def platform_smtp(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Configure the platform-wide SMTP default for one test, then reset."""
    from api.config import get_api_settings

    for key, value in {
        "PLATFORM_SMTP_HOST": "smtp.platform.test",
        "PLATFORM_SMTP_PORT": "2525",
        "PLATFORM_SMTP_USERNAME": "apikey",
        "PLATFORM_SMTP_PASSWORD": "platform-secret",
        "PLATFORM_SMTP_FROM_ADDRESS": "noreply@platform.test",
        "PLATFORM_SMTP_FROM_NAME": "Bookings",
    }.items():
        monkeypatch.setenv(key, value)
    get_api_settings.cache_clear()
    yield
    get_api_settings.cache_clear()


async def test_no_tenant_config_uses_platform_smtp_default(platform_smtp: Any) -> None:
    """A tenant with no email config row still sends -- via the platform sender."""
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=None)
    fake_provider = AsyncMock()
    fake_provider.send.return_value = DeliveryRef(provider="smtp", ref="<m@x>")

    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ) as mock_for:
        result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "sent"
    used = mock_for.call_args.args[0]
    assert (used.provider, used.smtp_host, used.smtp_port) == ("smtp", "smtp.platform.test", 2525)
    assert (used.from_address, used.from_name) == ("noreply@platform.test", "Bookings")
    assert (used.smtp_username, used.credentials) == ("apikey", "platform-secret")


async def test_disabled_tenant_config_is_not_overridden_by_platform_default(
    platform_smtp: Any,
) -> None:
    """A tenant that explicitly disabled email stays off -- no platform fallback."""
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=_log_config_row(enabled=False))

    result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "failed"
    assert job["last_error"] == "NOTIFICATION_NOT_CONFIGURED"


async def test_disabled_config_marks_failed_not_raised() -> None:
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=_log_config_row(enabled=False))

    result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "failed"
    assert job["status"] == "failed"


async def test_unknown_provider_marks_failed_not_raised() -> None:
    job = _job_row(status="pending")
    config = _log_config_row()
    config["provider"] = "carrier-pigeon"
    db = _StubDatabase(job=job, config=config)

    result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "failed"
    assert job["status"] == "failed"


async def test_missing_smtp_fields_marks_failed_not_raised() -> None:
    job = _job_row(status="pending")
    config = _smtp_config_row()
    config["smtp_host"] = None
    db = _StubDatabase(job=job, config=config)

    result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "failed"
    assert job["status"] == "failed"


# ==============================================================================
# Deterministic vs transient provider send errors
# ==============================================================================


async def test_deterministic_provider_error_marks_failed_not_raised() -> None:
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=_smtp_config_row())

    fake_provider = AsyncMock()
    fake_provider.send.side_effect = NotificationDeliveryError("smtp auth failed")

    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ):
        result = await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert result["status"] == "failed"
    assert job["status"] == "failed"
    assert job["attempts"] == 1  # a real send attempt was made


async def test_transient_provider_error_raises() -> None:
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=_smtp_config_row())

    fake_provider = AsyncMock()
    fake_provider.send.side_effect = ConnectionError("connection refused")

    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ):
        with pytest.raises(ConnectionError):
            await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    # Job remains pending -- redelivery retries the SAME job_id, no new row.
    assert job["status"] == "pending"


async def test_transient_error_records_the_attempt_and_reason_but_stays_pending() -> None:
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=_smtp_config_row())
    fake_provider = AsyncMock()
    fake_provider.send.side_effect = ConnectionError("connection unexpectedly closed")

    with patch("api.notifications.tasks.notification_provider_for", return_value=fake_provider):
        with pytest.raises(ConnectionError):
            await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert "connection unexpectedly closed" in job["last_error"]


async def test_transient_error_on_the_last_attempt_marks_failed_instead_of_hanging() -> None:
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=_smtp_config_row())
    fake_provider = AsyncMock()
    fake_provider.send.side_effect = ConnectionError("connection unexpectedly closed")

    with patch("api.notifications.tasks.notification_provider_for", return_value=fake_provider):
        result = await _execute(
            db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0, final_attempt=True,
        )

    assert result["status"] == "failed"
    assert job["status"] == "failed"
    assert job["last_error"].startswith("ConnectionError")


def test_task_retries_transient_errors_then_runs_a_final_attempt() -> None:
    """The Celery task really retries (it used to have no retry policy, so a
    transient error stranded the job at pending) and flags the last try."""
    from api.notifications.tasks import SEND_MAX_RETRIES, send_notification

    calls: list[bool] = []

    async def failing_run(job_id: str, tenant_id: str, *, final_attempt: bool = False) -> dict:
        calls.append(final_attempt)
        if final_attempt:
            return {"job_id": job_id, "status": "failed"}
        raise ConnectionError("down")

    with patch("api.notifications.tasks._run", failing_run):
        result = send_notification.apply(kwargs={"job_id": "job-1", "tenant_id": _TENANT_A})

    assert calls == [False] * SEND_MAX_RETRIES + [True]
    assert result.get() == {"job_id": "job-1", "status": "failed"}


# ==============================================================================
# Tenant re-scoping
# ==============================================================================


async def test_claims_built_from_claimed_rows_own_tenant() -> None:
    job = _job_row(tenant_id=_TENANT_B, status="pending")
    config = _log_config_row(tenant_id=_TENANT_B)
    db = _StubDatabase(job=job, config=config)

    fake_provider = AsyncMock()
    fake_provider.send.return_value = DeliveryRef(provider="log", ref="ref-1")

    captured_claims = []

    async def _capture_send(claims: Any, message: Any) -> DeliveryRef:
        captured_claims.append(claims)
        return DeliveryRef(provider="log", ref="ref-1")

    fake_provider.send.side_effect = _capture_send

    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ):
        result = await _execute(db, job_id="job-1", tenant_id=_TENANT_B, smtp_timeout=5.0)

    assert result["status"] == "sent"
    assert captured_claims[0].tenant_id == _TENANT_B


# ==============================================================================
# PII redaction -- no log line carries recipient/subject/body
# ==============================================================================


async def test_no_log_line_carries_pii(caplog: pytest.LogCaptureFixture) -> None:
    job = _job_row(status="pending")
    db = _StubDatabase(job=job, config=_log_config_row())

    fake_provider = AsyncMock()
    fake_provider.send.return_value = DeliveryRef(provider="log", ref="ref-1")

    with patch("api.notifications.tasks._log") as mock_log:
        with patch(
            "api.notifications.tasks.notification_provider_for", return_value=fake_provider
        ):
            await _execute(db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0)

    all_calls = mock_log.info.call_args_list + mock_log.warning.call_args_list
    for args, kwargs in all_calls:
        serialized = repr(args) + repr(kwargs)
        assert _PII_RECIPIENT not in serialized
        assert _PII_SUBJECT not in serialized
        assert _PII_BODY not in serialized


# ==============================================================================
# Channel-aware config fetch + timeout selection (S9.3)
# ==============================================================================


async def test_sms_job_fetches_sms_config_and_uses_twilio_timeout() -> None:
    job = _job_row(status="pending", channel="sms")
    db = _StubDatabase(job=job, config=_twilio_config_row(channel="sms"))

    from api.notifications.config_repository import NotificationConfig

    fake_config = NotificationConfig(
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
        credentials="fake-token-value",
        enabled=True,
    )

    fake_provider = AsyncMock()
    fake_provider.send.return_value = DeliveryRef(provider="twilio", ref="SM123")

    with (
        patch(
            "api.notifications.tasks.get_notification_config",
            AsyncMock(return_value=fake_config),
        ) as mock_get_config,
        patch(
            "api.notifications.tasks.notification_provider_for", return_value=fake_provider
        ) as mock_provider_for,
    ):
        result = await _execute(
            db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0, twilio_timeout=9.0
        )

    assert result["status"] == "sent"

    # channel-aware fetch: called with the job's own channel, "sms".
    call_args = mock_get_config.call_args
    assert call_args.args[-1] == "sms" or call_args.kwargs.get("channel") == "sms"

    # timeout selection: twilio provider -> twilio_timeout, not smtp_timeout.
    provider_call_kwargs = mock_provider_for.call_args.kwargs
    assert provider_call_kwargs["timeout"] == 9.0


async def test_email_path_fetches_email_config_with_smtp_timeout_regression() -> None:
    """Regression: the S9.1/S9.2 email path is unchanged after S9.3."""
    job = _job_row(status="pending", channel="email")
    db = _StubDatabase(job=job, config=_log_config_row())

    fake_provider = AsyncMock()
    fake_provider.send.return_value = DeliveryRef(provider="log", ref="ref-1")

    from api.notifications.config_repository import NotificationConfig

    fake_config = NotificationConfig(
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

    with (
        patch(
            "api.notifications.tasks.get_notification_config",
            AsyncMock(return_value=fake_config),
        ) as mock_get_config,
        patch(
            "api.notifications.tasks.notification_provider_for", return_value=fake_provider
        ) as mock_provider_for,
    ):
        result = await _execute(
            db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0, twilio_timeout=9.0
        )

    assert result["status"] == "sent"

    call_args = mock_get_config.call_args
    assert call_args.args[-1] == "email" or call_args.kwargs.get("channel") == "email"

    provider_call_kwargs = mock_provider_for.call_args.kwargs
    assert provider_call_kwargs["timeout"] == 5.0


async def test_sent_sms_job_is_no_op_provider_not_called() -> None:
    job = _job_row(status="sent", channel="sms")
    db = _StubDatabase(job=job, config=_twilio_config_row(channel="sms"))

    fake_provider = AsyncMock()
    with patch(
        "api.notifications.tasks.notification_provider_for", return_value=fake_provider
    ):
        result = await _execute(
            db, job_id="job-1", tenant_id=_TENANT_A, smtp_timeout=5.0, twilio_timeout=9.0
        )

    assert result["status"] == "no_op"
    fake_provider.send.assert_not_called()
