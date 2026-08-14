"""Shared Celery application instance for the chatbot platform.

One ``celery_app`` instance lives here. Both the API process (enqueues) and the
worker process (executes) import this module. The Celery CLI discovers it via
``-A api.tasks.celery_app``.

Conventions established here — inherited by every future task:
- JSON serialization only (pickle is an RCE vector — never add it to accept_content).
- Correlation-ID propagation: the enqueuer passes ``correlation_id`` as a kwarg;
  the task binds it into the common.logging ContextVar so the worker's JSON log
  carries the same ID as the originating HTTP request.
- task_acks_late + task_reject_on_worker_lost: a task killed mid-flight is
  redelivered, not silently lost (CLAUDE.md §3 "background tasks idempotent +
  retryable").
- Fail-fast on missing broker config: resolution raises at import/startup if no
  broker URL can be resolved (CLAUDE.md §3 config).
"""
from __future__ import annotations

from uuid import uuid4

from celery import Celery, Task
from common.logging import get_logger, log_context

from api.config import get_api_settings

_log = get_logger(__name__)


def _resolve_broker_url() -> str:
    """Return the broker URL, preferring CELERY_BROKER_URL then redis_url.

    Raises RuntimeError (fail-fast) if neither is configured — missing required
    config must never silently produce a non-functional app (CLAUDE.md §3).
    """
    settings = get_api_settings()
    url = settings.celery_broker_url or settings.redis_url
    if not url:
        raise RuntimeError(
            "Celery broker URL is not configured. "
            "Set CELERY_BROKER_URL (or REDIS_URL) in the environment. "
            "See deploy/.env.example for documentation."
        )
    return url


def _resolve_result_backend() -> str:
    """Return the result backend URL, preferring CELERY_RESULT_BACKEND then redis_url.

    Raises RuntimeError (fail-fast) if neither is configured.
    """
    settings = get_api_settings()
    url = settings.celery_result_backend or settings.redis_url
    if not url:
        raise RuntimeError(
            "Celery result backend URL is not configured. "
            "Set CELERY_RESULT_BACKEND (or REDIS_URL) in the environment. "
            "See deploy/.env.example for documentation."
        )
    return url


# -- Application instance ------------------------------------------------------
# Celery auto-detects a module-level name ``celery_app`` when the worker is
# launched with ``-A api.tasks.celery_app``.

celery_app = Celery(
    "chatbot",
    broker=_resolve_broker_url(),
    backend=_resolve_result_backend(),
    include=[
        "api.tasks.debug_tasks",
        "api.ingestion.tasks",
        "api.crm.tasks",
        "api.scheduling.tasks",
        "api.notifications.tasks",
        "api.notifications.events_tasks",
        "api.leads.tasks",
    ],
)

# -- Serialization (security: JSON only, never pickle) -------------------------
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Reliability: ack only after the task completes — redelivered if the worker
    # dies mid-flight, not silently lost.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Result housekeeping: expire stored results after 1 hour so Redis doesn't fill up.
    result_expires=3600,
    # Timezone.
    timezone="UTC",
    enable_utc=True,
    # Beat schedule — the reminder dispatcher (S8.3). Polls at
    # reminder_poll_interval_seconds; the task itself atomically claims due
    # rows (exactly-once gate lives in the claim SQL, not here).
    beat_schedule={
        "dispatch-due-reminders": {
            "task": "scheduling.dispatch_due_reminders",
            "schedule": get_api_settings().reminder_poll_interval_seconds,
        },
        # SR-21 D7: bound the in-console notifications feed, the first
        # append-only, write-on-every-lead, never-read-again table in the
        # product. Idempotent (a DELETE ... WHERE created_at < cutoff deletes
        # nothing extra on a redundant run) -- see api.notifications.events_tasks.
        "prune-notification-events": {
            "task": "notifications.prune_notification_events",
            "schedule": get_api_settings().notification_events_prune_interval_seconds,
        },
    },
)


# -- Base task with correlation-ID propagation ---------------------------------


class _CorrelationTask(Task):  # type: ignore[misc]
    """Abstract base task that binds ``correlation_id`` into the logging context.

    All tasks that use ``base=_CorrelationTask`` (or the ``@chatbot_task``
    decorator) automatically:
    1. Accept an optional ``correlation_id`` kwarg from the enqueuer.
    2. Bind it into the ``common.logging`` ContextVar (the same one the HTTP
       middleware uses) so worker JSON logs carry the same ID as the originating
       request — end-to-end trace across the queue boundary.
    3. If no ``correlation_id`` is supplied, generate one so logs are never
       context-less.

    Retry policy (default, overridable per-task):
    - autoretry_for: no automatic retry class set at the base; tasks declare
      their own retry_policy dict or ``autoretry_for`` for domain-specific errors.
    - max_retries: 3 (inherited by subclasses; override per-task as needed).
    - default_retry_delay: 5 s initial; callers should supply exponential backoff
      kwargs (``countdown``) when retrying manually.
    """

    abstract = True
    max_retries = 3
    default_retry_delay = 5

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Bind correlation_id before delegating to the task body."""
        correlation_id: str = str(kwargs.pop("correlation_id", None) or uuid4().hex)
        with log_context(correlation_id=correlation_id):
            return super().__call__(*args, **kwargs)
