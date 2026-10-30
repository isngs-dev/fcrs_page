"""Event + task dispatch bodies (the Celery tasks in ``agents.tasks`` are thin wrappers).

Claims are system-scoped; every claimed row is handled under claims rebuilt
from ITS ``tenant_id`` (never a neighbour's).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import AppException
from common.logging import get_logger, log_context

from api.agents import compliance, events, executors, outreach_repository, planner
from api.agents.repository import finish_run, start_run

_log = get_logger(__name__)

STOP_KINDS = frozenset({"opt_out_received", "human_takeover", "meeting_booked",
                        "email_complained"})


def system_claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="system:agents", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


# -- Events --------------------------------------------------------------------


async def _handle_event(db: Database, claims: AuthClaims, event: events.ClaimedEvent) -> str | None:
    """Run the event's handler. Returns a note stored on the done event (or ``None``)."""
    payload: dict[str, Any] = event.payload
    if event.kind == "outreach_requested":
        try:
            outcome = await planner.create_plan(
                db, claims, contact_id=payload["contact_id"], kind=payload["kind"],
                goal=payload.get("goal"), now=datetime.now(UTC),
            )
        except AppException as exc:
            if exc.code == "PLAN_ALREADY_ACTIVE":  # redelivery after a commit, or a race
                return "PLAN_ALREADY_ACTIVE"
            raise
        return None if outcome.plan_id else "NO_ALLOWED_CHANNEL"
    if event.kind == "call_completed":
        from api.call_agent.service import after_call  # noqa: PLC0415 -- cycle

        return await after_call(db, claims, payload)
    if event.kind == "inbound_lead_created":
        from api.agents.inbound_leads import handle_inbound_lead  # noqa: PLC0415

        return await handle_inbound_lead(db, claims, payload)
    if event.kind == "email_reply_received":
        # A3: classify + act; stops the plan itself (except out-of-office).
        from api.email_agent.inbound_service import handle_reply  # noqa: PLC0415 -- cycle

        return await handle_reply(db, claims, payload)
    if event.kind == "intake_meta_lead":  # A10 I7: fetch the lead from Meta, then ingest
        from api.intake.service import handle_meta_lead  # noqa: PLC0415 -- cycle

        return await handle_meta_lead(db, claims, payload)
    if event.kind == "email_bounced":  # A12 F3: e-mail is unusable; a permitted call isn't
        cancelled = await outreach_repository.cancel_channel_tasks(
            db, claims, payload["contact_id"], "email", reason="EMAIL_BOUNCED")
        await outreach_repository.stop_plan_if_empty(db, claims, payload["contact_id"],
                                                     reason="no_channel_left")
        return f"email_cancelled={cancelled}"
    if event.kind in STOP_KINDS:
        cancelled = await outreach_repository.stop_contact_plan(
            db, claims, payload["contact_id"], reason=event.kind)
        return f"cancelled={cancelled}"
    raise RuntimeError(f"No handler for agent event kind {event.kind!r}")


async def dispatch_events(db: Database, *, limit: int) -> dict[str, int]:
    claimed = await events.claim_due_events(db, limit=limit)
    counts = {"claimed": len(claimed), "done": 0, "retried": 0, "dead": 0}
    for event in claimed:
        with log_context(tenant_id=event.tenant_id,
                         correlation_id=event.correlation_id or event.event_id):
            try:
                note = await _handle_event(db, system_claims(event.tenant_id), event)
            except Exception as exc:  # noqa: BLE001 -- every failure is recorded on the row
                permanent = isinstance(exc, AppException) and exc.http_status < 500
                error = exc.code if isinstance(exc, AppException) else type(exc).__name__
                status = await events.mark_event_failed_attempt(
                    db, event, error=error, permanent=permanent)
                if status == "failed":
                    counts["dead"] += 1
                    _log.error("agent event dead-lettered", extra={
                        "event": "agent_event_dead_lettered", "kind": event.kind,
                        "reason": error, "attempt": event.attempts})
                else:
                    counts["retried"] += 1
                    _log.warning("agent event retry", extra={
                        "event": "agent_event_retry", "kind": event.kind, "reason": error,
                        "attempt": event.attempts})
                continue
            await events.mark_event_done(db, event, note=note)
            counts["done"] += 1
    return counts


# -- Tasks ---------------------------------------------------------------------


def gate_purpose(task: outreach_repository.ClaimedTask) -> compliance.Purpose:
    """The inbound first email, or the call to someone who just shared their number in the
    chat, answers them (no cadence hold); the thank-you follows a call; the rest is outreach."""
    if task.channel == "call" and task.intent == planner.CHAT_CALL_INTENT:
        return "first_response"
    if task.channel == "email" and task.step_index == 0 and task.plan_kind == "inbound_auto":
        return "first_response"
    return "after_call" if task.goal == "thank_you" else "outreach"


async def chat_call_wait(db: Database, claims: AuthClaims, task: outreach_repository.ClaimedTask,
                         now: datetime) -> datetime | None:
    """When a chat call should wait until: ``CHAT_IDLE`` after the visitor's last chat
    message, unless the chat ended with our goodbye. ``None`` = ring now."""
    if task.channel != "call" or task.intent != planner.CHAT_CALL_INTENT:
        return None
    from api.agents.memory import visitor_ids  # noqa: PLC0415 -- cycle
    from api.conversation_store.repository import latest_chat_message  # noqa: PLC0415
    from api.orchestrator.identity_capture import GOODBYE_INTENT  # noqa: PLC0415

    last = await latest_chat_message(db, claims, await visitor_ids(db, claims, task.contact_id))
    if last is None or last.intent == GOODBYE_INTENT:
        return None
    quiet_at = last.created_at + planner.CHAT_IDLE
    return quiet_at if quiet_at > now else None


async def _run_task(db: Database, task: outreach_repository.ClaimedTask, now: datetime) -> str:
    claims = system_claims(task.tenant_id)
    channel = task.channel
    wait = await chat_call_wait(db, claims, task, now)
    if wait is not None:  # still chatting: ring once they've gone quiet (no attempt used)
        await outreach_repository.reschedule_task(db, claims, task.task_id, at=wait,
                                                  reason="VISITOR_CHATTING")
        return "chatting"
    run_id = await start_run(db, claims, agent="email" if channel == "email" else "call")
    gate = await compliance.check(db, claims, contact_id=task.contact_id,
                                  channel=channel, now=now, run_id=run_id,  # type: ignore[arg-type]
                                  purpose=gate_purpose(task))
    if not gate.ok:
        code = gate.code or "GATE_BLOCKED"
        if code in compliance.PERMANENT_CODES:
            await outreach_repository.finish_task(db, claims, task.task_id, status="cancelled",
                                                  error=code)
        else:
            await outreach_repository.reschedule_task(
                db, claims, task.task_id, at=gate.retry_at or now + compliance.DEFAULT_RETRY,
                reason=code)
        await finish_run(db, claims, run_id, ok=True)
        return f"gate:{code}"

    executor = executors.get_executor(channel)
    if executor is None:  # unregistered between claim and now
        await outreach_repository.reschedule_task(db, claims, task.task_id, at=now,
                                                  reason="NO_EXECUTOR")
        await finish_run(db, claims, run_id, ok=False)
        return "no_executor"

    attempts = await outreach_repository.mark_task_attempt(db, claims, task.task_id)
    try:
        result = await executor(db, claims, task, run_id)
    except executors.DeferTask as defer:
        await outreach_repository.refund_attempt(db, claims, task.task_id)
        await outreach_repository.reschedule_task(db, claims, task.task_id,
                                                  at=now + defer.delay, reason=defer.code)
        await finish_run(db, claims, run_id, ok=True)
        return f"gate:{defer.code}"
    except AppException as exc:
        if exc.http_status < 500:  # deterministic (bad input/config, refused): never retried
            await outreach_repository.finish_task(db, claims, task.task_id, status="failed",
                                                  error=exc.code)
            await finish_run(db, claims, run_id, ok=False)
            return "failed"
        # A9 drill: an upstream outage (e.g. LLM_ERROR, 502) is retried like any transient
        # error -- with its code recorded -- and fails only after max_attempts.
        return await _retry_or_fail(db, claims, task, run_id, attempts, now, exc.code)
    except Exception as exc:  # noqa: BLE001 -- transient: retry until max_attempts
        return await _retry_or_fail(db, claims, task, run_id, attempts, now, type(exc).__name__)
    await outreach_repository.finish_task(db, claims, task.task_id, status=result.status,
                                          result=result.result, error=result.error)
    await finish_run(db, claims, run_id, ok=result.status != "failed")
    return result.status


async def _retry_or_fail(db: Database, claims: AuthClaims,
                         task: outreach_repository.ClaimedTask, run_id: str, attempts: int,
                         now: datetime, error: str) -> str:
    if attempts >= task.max_attempts:
        await outreach_repository.finish_task(db, claims, task.task_id, status="failed",
                                              error=error)
    else:
        await outreach_repository.reschedule_task(
            db, claims, task.task_id, at=now + events.backoff(attempts), reason=error)
    await finish_run(db, claims, run_id, ok=False)
    return "retry" if attempts < task.max_attempts else "failed"


async def dispatch_due_tasks(db: Database, *, limit: int, now: datetime | None = None
                             ) -> dict[str, int]:
    now = now or datetime.now(UTC)
    claimed = await outreach_repository.claim_due_tasks(
        db, channels=executors.registered_channels(), limit=limit, now=now)
    outcomes: dict[str, int] = {"claimed": len(claimed)}
    for task in claimed:
        with log_context(tenant_id=task.tenant_id):
            outcome = await _run_task(db, task, now)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


async def reap(db: Database, *, lease_seconds: int) -> dict[str, int]:
    return {
        "tasks": await outreach_repository.reap_stale_tasks_system(db, lease_seconds=lease_seconds),
        "events": await events.reap_stale_events_system(db, lease_seconds=lease_seconds),
    }


