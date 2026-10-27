"""Celery Beat tasks for the agent foundations (queue ``agents`` via ``task_routes``).

Same loop-per-invocation + ``Database.connect(..., statement_cache_size=0)``
shape as ``api.scheduling.tasks``. Exactly-once lives in the claim SQL; these
wrappers only open a connection and delegate to ``api.agents.dispatch``.

correlation_id must be declared in every signature (see ``api.scheduling.tasks``).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from common.db import Database
from common.logging import get_logger
from common.pgvector import register_vector_init

import api.call_agent.executor  # noqa: F401 -- registers the call executor (A6)
import api.email_agent.executor  # noqa: F401 -- registers the email executor (A2)
from api.agents import dispatch
from api.agents.outreach_repository import end_campaigns_system
from api.audit.chain import seal_audit_chain
from api.call_agent.service import purge_expired_recordings, reap_stale_calls
from api.email_agent.service import expire_drafts
from api.tasks.celery_app import _CorrelationTask, celery_app

_log = get_logger(__name__)


def _run(body: Callable[[Database, Any], Awaitable[Any]]) -> Any:
    async def _inner() -> Any:
        from api.config import get_api_settings  # noqa: PLC0415

        settings = get_api_settings()
        # The email composer and reply answering search the knowledge base from these
        # tasks: without the pgvector codec every query fails (DataError, list vs str).
        db = await Database.connect(settings.database_url, statement_cache_size=0,
                                    init=register_vector_init)
        try:
            return await body(db, settings)
        finally:
            await db.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_inner())
    finally:
        loop.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, name="agents.dispatch_events", base=_CorrelationTask)
def dispatch_events(
    self: _CorrelationTask,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, int]:
    result: dict[str, int] = _run(
        lambda db, s: dispatch.dispatch_events(db, limit=s.agent_dispatch_batch_size))
    if result["claimed"]:
        _log.info("agent events dispatched", extra={"event": "agent_events_dispatched"})
    return result


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, name="agents.dispatch_due_tasks", base=_CorrelationTask)
def dispatch_due_tasks(
    self: _CorrelationTask,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, int]:
    result: dict[str, int] = _run(
        lambda db, s: dispatch.dispatch_due_tasks(db, limit=s.agent_dispatch_batch_size))
    return result


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, name="agents.reap_stale", base=_CorrelationTask)
def reap_stale(
    self: _CorrelationTask,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, int]:
    async def body(db: Database, s: Any) -> dict[str, int]:
        counts = await dispatch.reap(db, lease_seconds=s.agent_task_lease_seconds)
        counts["drafts"] = await expire_drafts(db, ttl_days=s.email_draft_ttl_days)
        counts["calls"] = await reap_stale_calls(db, grace_seconds=s.call_reap_grace_seconds)
        # A12 F5: campaigns past their end date stop their plans.
        counts["campaign_plans"] = await end_campaigns_system(db, now=datetime.now(UTC))
        return counts

    result: dict[str, int] = _run(body)
    if any(result.values()):
        _log.warning("agent leases expired", extra={"event": "agent_leases_reaped"})
    return result


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, name="agents.seal_audit_chain", base=_CorrelationTask)
def seal_audit(
    self: _CorrelationTask,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> int:
    sealed: int = _run(lambda db, _s: seal_audit_chain(db))
    return sealed


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, name="agents.purge_recordings", base=_CorrelationTask)
def purge_recordings(
    self: _CorrelationTask,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, int]:
    """A9 H8: daily -- recordings past each tenant's retention are deleted at Plivo."""
    result: dict[str, int] = _run(lambda db, _s: purge_expired_recordings(db))
    if result["failed"]:
        _log.warning("some recordings could not be purged",
                     extra={"event": "call_recording_purge_incomplete"})
    return result
