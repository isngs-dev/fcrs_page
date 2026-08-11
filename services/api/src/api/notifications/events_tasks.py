"""Celery Beat task: prune old ``notification_events`` rows (SR-21 D7).

Deliberately a SEPARATE module from ``api.notifications.tasks`` (the
OUTBOUND ``send_notification`` task, S9.1) -- D1's naming/module separation
applies to task modules too, not just tables: this file only ever touches
``notification_events``/``notification_event_reads`` and never reads or
writes ``notification_jobs``.

``notification_events`` is the first append-only, write-on-every-lead,
never-read-again table in the product (D7's rationale) -- unbounded, it
becomes the largest table in the database. This task keeps it bounded by
deleting rows older than ``notification_events_retention_days`` (a Pydantic
Settings value, default 90 days). ``notification_event_reads`` rows for
deleted events are removed automatically via the migration's composite
``ON DELETE CASCADE`` foreign key -- no separate delete needed here.

Idempotent + retryable (CLAUDE.md §3): a DELETE ... WHERE created_at < cutoff
is naturally idempotent (running it twice deletes nothing extra the second
time), and any transient DB error propagates so Celery's own retry/backoff
applies -- there is no partial-completion state to corrupt.
"""
from __future__ import annotations

import asyncio

from common.logging import get_logger

from api.notifications.events_repository import prune_events_older_than
from api.tasks.celery_app import _CorrelationTask, celery_app

_log = get_logger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="notifications.prune_notification_events",
    base=_CorrelationTask,
)
def prune_notification_events(
    self: _CorrelationTask,
    *,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, object]:
    """Delete ``notification_events`` rows past the retention window.

    Runs on Celery Beat's schedule (see ``api.tasks.celery_app``), not on
    any HTTP request path -- it is system-scoped maintenance, never
    triggered by or blocking a caller's mutation (mirrors D2's fail-open
    posture for emit: this is a separate, best-effort background sweep).
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


async def _run() -> dict[str, object]:
    from common.db import Database  # noqa: PLC0415

    from api.config import get_api_settings  # noqa: PLC0415

    settings = get_api_settings()
    db = await Database.connect(settings.database_url, statement_cache_size=0)
    try:
        deleted = await prune_events_older_than(
            db, retention_days=settings.notification_events_retention_days
        )
        _log.info(
            "notification_events pruned",
            extra={
                "event": "notification_events_pruned",
                "task": "notifications.prune_notification_events",
                "deleted_count": deleted,
            },
        )
        return {"deleted": deleted}
    finally:
        await db.close()
