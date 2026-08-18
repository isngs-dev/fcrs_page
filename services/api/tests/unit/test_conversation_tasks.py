"""Unit tests for api.conversation_store.tasks (conversation_store.close_idle_conversations).

Covers:
- _execute_sweep calls close_idle_conversations with the configured
  idle_minutes and returns {"closed": <count>}.
- Zero closed -> no log call (avoid log noise on every quiet tick).
- Some closed -> one info log call, PII-safe (count/idle_minutes only).
- correlation_id declared on the task signature (S5.1 regression guard,
  mirrors test_scheduling_reminder_tasks.py's own regression test).
"""
from __future__ import annotations

import sys
from unittest.mock import ANY, AsyncMock, patch

from api.conversation_store.repository import ClosedConversation

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


def _reset_modules() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("api.conversation_store") or key.startswith("api.tasks"):
            del sys.modules[key]
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


class _StubDatabase:
    async def close(self) -> None:
        pass


async def test_execute_sweep_calls_repository_with_configured_idle_minutes() -> None:
    _reset_modules()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        closed = [ClosedConversation(conversation_id="conv-1", tenant_id="tenant-a")]

        with patch(
            "api.conversation_store.tasks.close_idle_conversations",
            AsyncMock(return_value=closed),
        ) as mock_close:
            from api.conversation_store.tasks import _execute_sweep  # noqa: PLC0415

            result = await _execute_sweep(_StubDatabase(), idle_minutes=30)  # type: ignore[arg-type]

        mock_close.assert_awaited_once_with(ANY, idle_minutes=30)

    assert result == {"closed": 1}


async def test_execute_sweep_zero_closed_no_log_call() -> None:
    _reset_modules()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        with (
            patch(
                "api.conversation_store.tasks.close_idle_conversations",
                AsyncMock(return_value=[]),
            ),
            patch("api.conversation_store.tasks._log") as mock_log,
        ):
            from api.conversation_store.tasks import _execute_sweep  # noqa: PLC0415

            result = await _execute_sweep(_StubDatabase(), idle_minutes=30)  # type: ignore[arg-type]

    assert result == {"closed": 0}
    mock_log.info.assert_not_called()


async def test_execute_sweep_some_closed_logs_pii_safe_count() -> None:
    _reset_modules()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        closed = [
            ClosedConversation(conversation_id="conv-1", tenant_id="tenant-a"),
            ClosedConversation(conversation_id="conv-2", tenant_id="tenant-b"),
        ]

        with (
            patch(
                "api.conversation_store.tasks.close_idle_conversations",
                AsyncMock(return_value=closed),
            ),
            patch("api.conversation_store.tasks._log") as mock_log,
        ):
            from api.conversation_store.tasks import _execute_sweep  # noqa: PLC0415

            result = await _execute_sweep(_StubDatabase(), idle_minutes=30)  # type: ignore[arg-type]

    assert result == {"closed": 2}
    mock_log.info.assert_called_once()
    _, kwargs = mock_log.info.call_args
    extra = kwargs["extra"]
    assert extra["count"] == 2
    assert extra["idle_minutes"] == 30
    # PII-safe: no conversation_id/tenant_id/message content in the log extras.
    assert "conversation_id" not in extra
    assert "tenant_id" not in extra


def test_close_idle_conversations_task_delay_accepts_correlation_id() -> None:
    _reset_modules()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        import api.conversation_store.tasks  # noqa: PLC0415, F401
        import api.tasks.celery_app as capp  # noqa: PLC0415

        capp.celery_app.conf.task_always_eager = True
        capp.celery_app.conf.task_eager_propagates = False

        from api.conversation_store.tasks import close_idle_conversations_task  # noqa: PLC0415

        with patch("api.conversation_store.tasks.asyncio.new_event_loop") as mock_loop:
            mock_event_loop = mock_loop.return_value
            mock_event_loop.run_until_complete.return_value = {"closed": 0}
            mock_event_loop.close.return_value = None

            result = close_idle_conversations_task.delay(correlation_id="cid-sweep-test")
            assert result is not None
