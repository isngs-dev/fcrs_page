"""Celery task: conversation_store.close_idle_conversations (SR-25).

Same asyncio-loop-per-invocation + ``Database.connect(..., statement_cache_size=0)``
shape as ``api.scheduling.tasks``/``api.crm.tasks`` (S5.1 pattern).

The Celery Beat periodic task itself: unlike ``scheduling.dispatch_due_reminders``
(claim rows, then ``.delay()`` one follow-up task per row), this IS the whole
unit of work in one call -- ``close_idle_conversations``'s single
``UPDATE ... RETURNING`` both selects and closes idle conversations, so there
is nothing left to dispatch per row.

correlation_id (S5.1 rule): MUST be declared in the task signature. Celery runs
``check_arguments`` inside ``apply_async`` at enqueue time, before the base
``_CorrelationTask.__call__`` can consume it. Omitting it makes Beat's
schedule-driven ``apply_async`` raise ``TypeError`` at enqueue.
"""
from __future__ import annotations

import asyncio

from common.db import Database
from common.logging import get_logger

from api.conversation_store.repository import ClosedConversation, close_idle_conversations
from api.tasks.celery_app import _CorrelationTask, celery_app

_log = get_logger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="conversation_store.close_idle_conversations",
    base=_CorrelationTask,
)
def close_idle_conversations_task(
    self: _CorrelationTask,
    correlation_id: str | None = None,  # noqa: ARG001 -- consumed by _CorrelationTask.__call__
) -> dict[str, object]:
    """Beat periodic task: close every conversation idle past the configured timeout.

    Returns
    -------
    dict
        ``{"closed": <count>}``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run_sweep())
    finally:
        loop.close()


async def _run_sweep() -> dict[str, object]:
    """Async inner body: open a DB connection and delegate to ``_execute_sweep``."""
    from api.config import get_api_settings  # noqa: PLC0415

    settings = get_api_settings()
    db = await Database.connect(settings.database_url, statement_cache_size=0)
    try:
        return await _execute_sweep(db, idle_minutes=settings.conversation_idle_timeout_minutes)
    finally:
        await db.close()


async def _execute_sweep(db: Database, *, idle_minutes: int) -> dict[str, object]:
    """Core sweep: close idle conversations and log the count (PII-safe --
    conversation_id/tenant_id only, never message content)."""
    closed: list[ClosedConversation] = await close_idle_conversations(db, idle_minutes=idle_minutes)

    if closed:
        _log.info(
            "idle conversations closed",
            extra={
                "event": "conversations_idle_closed",
                "count": len(closed),
                "idle_minutes": idle_minutes,
            },
        )

    return {"closed": len(closed)}
