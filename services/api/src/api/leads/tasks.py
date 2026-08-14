"""Celery task: leads.classify_lead_email.

Auto-classifies a captured lead's email (``api.leads.email_classification``)
and, for a definitive verdict, moves the lead's pipeline stage the same way
an agent's manual ``PATCH /admin/leads/{lead_id}`` would -- reusing the SAME
``validate_transition``/``update_lead_stage``/``add_activity``/``record_audit``/
``emit_event_safe`` sequence ``api.leads.admin_routes._patch_lead_stage`` uses,
just with a system actor instead of an HTTP caller. Mirrors
``api.crm.tasks.sync_lead``'s structure exactly (S7.4 decision 3's Celery-
task-never-blocks-capture pattern, extended here to email qualification):

  1. Builds a tenant-scoped service ``AuthClaims`` (``subject=
     "system:lead-qualification"``, ``role=CLIENT_ADMIN``) from the trusted
     ``tenant_id`` kwarg -- never from visitor input.
  2. Loads the lead. Missing lead -> no-op success (nothing to classify).
  3. No email at all (``lead.email is None`` -- an anonymous SR-9.1
     booking-created lead) -> no-op success. This is NOT the same as a bad
     email; classification never runs for a lead that was never given one,
     so it stays in Captured for a human to follow up rather than being
     auto-disqualified for "no email" (that would conflate "nothing was
     captured" with "something bad was captured").
  4. ``classify_email`` never raises -- every input maps to a verdict, so
     there is no deterministic-vs-transient split to make around it. The
     one thing that CAN raise transiently is the DB write itself; that
     propagates untouched so Celery retries.
  5. The verdict/reason is always persisted (``update_lead_email_verdict``).
     A stage move only happens for a definitive verdict (qualified/
     disqualified) AND only if ``validate_transition`` still accepts it from
     the lead's CURRENT stage -- if an agent already manually moved the lead
     between this task's ``get_lead`` and now, the transition is rejected
     and this task skips the move (the verdict is still recorded) rather
     than clobbering a human decision.

correlation_id (S5.1 rule): MUST be declared in the signature, same reason
``sync_lead`` documents -- Celery's ``check_arguments`` runs at enqueue time,
before ``_CorrelationTask.__call__`` can consume it.

PII discipline: log lines here carry only ``lead_id``/``tenant_id``/
``verdict``/``reason`` (a classification outcome, not PII) -- never the
email address itself.
"""
from __future__ import annotations

import asyncio
import dataclasses

from common.auth import AuthClaims, Role
from common.cache import build_cache
from common.db import Database
from common.errors import ValidationError
from common.logging import get_logger

from api.audit.repository import record_audit
from api.leads.email_classification import MxResult, classify_email
from api.leads.mx_check import check_mx_cached
from api.leads.pipeline import compute_qualification_score, status_for_stage, validate_transition
from api.leads.repository import (
    add_activity,
    get_lead,
    update_lead_email_verdict,
    update_lead_stage,
)
from api.notifications.emit import emit_event_safe
from api.tasks.celery_app import _CorrelationTask, celery_app

_log = get_logger(__name__)

_SYSTEM_ACTOR = "system:lead-qualification"


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="leads.classify_lead_email",
    base=_CorrelationTask,
)
def classify_lead_email(
    self: _CorrelationTask,
    *,
    tenant_id: str,
    lead_id: str,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, object]:
    """Classify a single lead's email and, if definitive, auto-move its stage.

    Parameters
    ----------
    tenant_id:
        Trusted tenant identifier. Originates from ``claims.tenant_id`` at
        enqueue time -- never from visitor input.
    lead_id:
        The ``leads.lead_id`` to classify.
    correlation_id:
        Must be declared here (see module docstring).

    Returns
    -------
    dict
        ``{"lead_id": ..., "status": "succeeded"|"no_op", "verdict": ...,
        "stage_moved": bool}``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run(tenant_id, lead_id))
    finally:
        loop.close()


async def _run(tenant_id: str, lead_id: str) -> dict[str, object]:
    """Async inner body: open a DB connection and delegate to ``_execute``."""
    from api.config import get_api_settings  # noqa: PLC0415

    settings = get_api_settings()
    db = await Database.connect(settings.database_url, statement_cache_size=0)
    try:
        return await _execute(db, tenant_id, lead_id)
    finally:
        await db.close()


async def _execute(db: Database, tenant_id: str, lead_id: str) -> dict[str, object]:
    """Core classify -> persist verdict -> maybe-move-stage logic."""
    from api.config import get_api_settings  # noqa: PLC0415

    claims = AuthClaims(subject=_SYSTEM_ACTOR, role=Role.CLIENT_ADMIN, tenant_id=tenant_id)

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        _log.warning(
            "lead_email_classify_lead_missing",
            extra={
                "event": "lead_email_classify_lead_missing",
                "lead_id": lead_id,
                "tenant_id": tenant_id,
            },
        )
        return {"lead_id": lead_id, "status": "no_op", "verdict": None, "stage_moved": False}

    if lead.email is None:
        _log.info(
            "lead_email_classify_skipped_no_email",
            extra={
                "event": "lead_email_classify_skipped_no_email",
                "lead_id": lead_id,
                "tenant_id": tenant_id,
            },
        )
        return {"lead_id": lead_id, "status": "no_op", "verdict": None, "stage_moved": False}

    settings = get_api_settings()
    cache = build_cache(settings.redis_url)

    async def _mx_checker(domain: str) -> MxResult:
        return await check_mx_cached(
            cache, claims, domain, timeout_seconds=settings.email_mx_check_timeout_seconds,
        )

    classification = await classify_email(lead.email, mx_checker=_mx_checker)

    await update_lead_email_verdict(
        db, claims, lead_id, verdict=classification.verdict, reason=classification.reason,
    )

    stage_moved = False
    if classification.verdict in ("qualified", "disqualified"):
        try:
            validate_transition(lead.stage, classification.verdict)
        except ValidationError:
            # The lead already moved on (e.g. an agent manually advanced or
            # disqualified it between our get_lead above and now). The
            # verdict is still recorded; auto-moving stage here would
            # silently clobber a human decision or violate the state
            # machine, so this task simply does not.
            _log.info(
                "lead_email_classify_stage_move_skipped",
                extra={
                    "event": "lead_email_classify_stage_move_skipped",
                    "lead_id": lead_id,
                    "tenant_id": tenant_id,
                    "current_stage": lead.stage,
                    "verdict": classification.verdict,
                },
            )
        else:
            new_status = status_for_stage(classification.verdict)
            scored_lead = dataclasses.replace(
                lead, stage=classification.verdict, status=new_status,
            )
            new_score = compute_qualification_score(scored_lead)

            await update_lead_stage(
                db,
                claims,
                lead_id,
                stage=classification.verdict,
                status=new_status,
                qualification_score=new_score,
            )
            await add_activity(
                db,
                claims,
                lead_id,
                type="stage_change",
                payload={
                    "from_stage": lead.stage,
                    "to_stage": classification.verdict,
                    "reason": classification.reason,
                },
                actor=_SYSTEM_ACTOR,
            )
            await record_audit(
                db,
                claims,
                action="lead_stage_transitioned",
                target_type="lead",
                target_id=lead_id,
                metadata={
                    "from_stage": lead.stage,
                    "to_stage": classification.verdict,
                    "reason": classification.reason,
                },
            )
            await emit_event_safe(
                db,
                claims,
                kind="lead_stage_transitioned",
                category="leads",
                target_type="lead",
                target_id=lead_id,
                payload={
                    "lead_id": lead_id,
                    "from_stage": lead.stage,
                    "to_stage": classification.verdict,
                },
                actor_id=_SYSTEM_ACTOR,
            )
            stage_moved = True

    _log.info(
        "lead_email_classified",
        extra={
            "event": "lead_email_classified",
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "verdict": classification.verdict,
            "reason": classification.reason,
            "stage_moved": stage_moved,
        },
    )
    return {
        "lead_id": lead_id,
        "status": "succeeded",
        "verdict": classification.verdict,
        "stage_moved": stage_moved,
    }
