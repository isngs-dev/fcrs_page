"""Unit tests for Celery configuration and broker/backend resolver.

Covers:
- accept_content == ["json"] (pickle NOT accepted — security regression guard).
- task_serializer and result_serializer are "json".
- task_acks_late and task_reject_on_worker_lost are True.
- Broker/backend resolver returns redis_url when celery-specific envs are unset.
- Broker/backend resolver raises (fail-fast) when neither celery env nor redis_url is set.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

# -- Isolation -----------------------------------------------------------------
#
# The tests below deliberately mutate GLOBAL interpreter state to import
# ``api.tasks.celery_app`` under a patched environment: they delete
# ``api.config`` / ``api.tasks*`` from ``sys.modules``, clear settings caches,
# and reload. Without cleanup that state leaks and poisons later, unrelated
# tests (rate-limiting, password-reset) that read settings via the cached
# factory. This autouse fixture snapshots the affected modules + settings caches
# and restores them after every test in this module, keeping the pollution local.

_AFFECTED = lambda k: k.startswith("api.tasks") or k == "api.config"  # noqa: E731


@pytest.fixture(autouse=True)
def _restore_module_and_settings_state() -> object:
    saved = {k: v for k, v in sys.modules.items() if _AFFECTED(k)}
    try:
        yield
    finally:
        for k in [k for k in sys.modules if _AFFECTED(k)]:
            del sys.modules[k]
        sys.modules.update(saved)
        from common.settings import get_settings

        get_settings.cache_clear()
        try:
            from api.config import get_api_settings

            get_api_settings.cache_clear()
        except ImportError:
            pass


# -- Helpers -------------------------------------------------------------------

_BASE_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


def _load_celery_app(extra_env: dict[str, str]) -> object:
    """Import celery_app fresh with the given env overrides.

    Must clear the lru_cache on ApiSettings and re-import the module so the
    settings are evaluated with the test-supplied env.
    """
    import importlib
    import sys

    # Remove cached modules so the import re-runs.
    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    # Also clear the lru_cache on the settings factory.
    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, **extra_env}
    with patch.dict("os.environ", env, clear=True):
        # Re-import after env is patched.
        import api.tasks.celery_app as mod  # noqa: PLC0415

        importlib.reload(mod)
        return mod.celery_app


# ==============================================================================
# Serialization — pickle must NOT be in accept_content (security)
# ==============================================================================


def test_accept_content_is_json_only() -> None:
    """accept_content must be exactly ["json"]; pickle is an RCE vector."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379"}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert app.conf.accept_content == ["json"], (
        f"accept_content must be ['json'], got {app.conf.accept_content!r}; "
        "pickle is an RCE vector — never add it."
    )
    assert "pickle" not in app.conf.accept_content


def test_serializers_are_json() -> None:
    """task_serializer and result_serializer must both be 'json'."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379"}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert app.conf.task_serializer == "json"
    assert app.conf.result_serializer == "json"


# ==============================================================================
# Reliability config
# ==============================================================================


def test_acks_late_and_reject_on_worker_lost() -> None:
    """task_acks_late and task_reject_on_worker_lost must both be True."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379"}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert app.conf.task_acks_late is True, "task_acks_late must be True (retryable tasks)"
    assert app.conf.task_reject_on_worker_lost is True, (
        "task_reject_on_worker_lost must be True (no silent task loss)"
    )


# ==============================================================================
# Broker/backend resolver
# ==============================================================================


def test_resolver_uses_redis_url_when_celery_envs_unset() -> None:
    """When CELERY_BROKER_URL / CELERY_RESULT_BACKEND are unset, redis_url is used."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    redis = "redis://stub-host:6379"
    env = {**_BASE_ENV, "REDIS_URL": redis}
    # Ensure celery-specific envs are absent.
    env.pop("CELERY_BROKER_URL", None)
    env.pop("CELERY_RESULT_BACKEND", None)

    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    # Celery normalises the broker transport; check that the redis host is present.
    assert "stub-host" in (app.conf.broker_url or ""), (
        f"broker_url should contain the redis_url host, got {app.conf.broker_url!r}"
    )
    assert "stub-host" in (app.conf.result_backend or ""), (
        f"result_backend should contain the redis_url host, got {app.conf.result_backend!r}"
    )


def test_resolver_uses_celery_specific_envs_when_set() -> None:
    """CELERY_BROKER_URL and CELERY_RESULT_BACKEND override redis_url."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    broker = "redis://broker-host:6379/1"
    backend = "redis://backend-host:6379/2"
    env = {
        **_BASE_ENV,
        "REDIS_URL": "redis://default-host:6379",
        "CELERY_BROKER_URL": broker,
        "CELERY_RESULT_BACKEND": backend,
    }

    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert "broker-host" in (app.conf.broker_url or "")
    assert "backend-host" in (app.conf.result_backend or "")


def test_resolver_raises_when_neither_celery_env_nor_redis_url_set() -> None:
    """Fail-fast: _resolve_broker_url raises RuntimeError when settings provide no URL.

    We mock get_api_settings to return a settings object with redis_url=None and
    no celery-specific URLs — this is the authoritative test of the guard condition
    regardless of any .env file present in the project.
    """
    from unittest.mock import MagicMock, patch

    import pytest

    from api.tasks.celery_app import _resolve_broker_url  # noqa: PLC0415

    # Build a minimal mock that mimics ApiSettings with all URL fields as None.
    stub_settings = MagicMock()
    stub_settings.celery_broker_url = None
    stub_settings.celery_result_backend = None
    stub_settings.redis_url = None

    with patch("api.tasks.celery_app.get_api_settings", return_value=stub_settings):
        with pytest.raises(RuntimeError, match="not configured"):
            _resolve_broker_url()


# ==============================================================================
# Beat schedule -- dispatch-due-reminders (S8.3)
# ==============================================================================


def test_beat_schedule_contains_dispatch_due_reminders() -> None:
    """beat_schedule has the reminder-dispatch periodic task at the configured interval."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    poll_interval = "45"
    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379", "REMINDER_POLL_INTERVAL_SECONDS": poll_interval}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    entry = app.conf.beat_schedule.get("dispatch-due-reminders")
    assert entry is not None, "beat_schedule must contain 'dispatch-due-reminders'"
    assert entry["task"] == "scheduling.dispatch_due_reminders"
    assert entry["schedule"] == 45


def test_scheduling_tasks_module_is_in_include() -> None:
    """api.scheduling.tasks must be in the Celery app's include list so the
    worker discovers scheduling.dispatch_due_reminders / scheduling.send_reminder."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379"}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert "api.scheduling.tasks" in app.conf.include


# ==============================================================================
# Notifications include (S9.1) -- no beat_schedule change
# ==============================================================================


def test_notifications_tasks_module_is_in_include() -> None:
    """api.notifications.tasks must be in the Celery app's include list so the
    worker discovers notifications.send_notification."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379"}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert "api.notifications.tasks" in app.conf.include


def test_beat_schedule_unchanged_by_notifications() -> None:
    """beat_schedule must still contain ONLY dispatch-due-reminders plus
    SR-21's prune-notification-events -- S9.1's OUTBOUND notifications
    (notification_jobs) are still enqueued on demand, not polled by Beat;
    the only Beat addition since S9.1 is SR-21 D7's IN-CONSOLE feed
    retention sweep (notification_events), a completely separate table
    (D1) with its own periodic maintenance need."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379"}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert list(app.conf.beat_schedule.keys()) == [
        "dispatch-due-reminders",
        "prune-notification-events",
    ]


def test_notification_events_tasks_module_is_in_include() -> None:
    """api.notifications.events_tasks must be in the Celery app's include
    list so the worker discovers notifications.prune_notification_events
    (SR-21 D7) -- a separate module from api.notifications.tasks (D1's
    module-level separation, not just table-level)."""
    import sys
    from unittest.mock import patch

    for key in list(sys.modules.keys()):
        if key.startswith("api.tasks") or key == "api.config":
            del sys.modules[key]

    from common.settings import get_settings

    get_settings.cache_clear()

    env = {**_BASE_ENV, "REDIS_URL": "redis://stub-host:6379"}
    with patch.dict("os.environ", env, clear=True):
        import api.tasks.celery_app as mod  # noqa: PLC0415

        app = mod.celery_app

    assert "api.notifications.events_tasks" in app.conf.include
