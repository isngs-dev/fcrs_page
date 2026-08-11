"""Fail-open emit wrapper for the in-console notifications feed (SR-21 D2).

``emit_event_safe`` is the ONLY way any mutation call site should write a
``notification_events`` row. It wraps
``api.notifications.events_repository.emit_event`` and guarantees:

- The feed write happens strictly AFTER the caller's observed mutation is
  already durably committed -- this module never participates in that
  transaction and never gates it.
- ANY exception (not just a narrow subclass) raised by the underlying insert
  is caught here and NEVER propagates. The caller always gets a normal
  return -- either the new ``event_id`` on success, or ``None`` on failure.
- A swallowed failure is never silent: it is logged at WARNING with
  ``event``/``tenant_id``/``kind`` (D2's mandated, tested log shape --
  ``kind`` is allowlisted in ``common.logging._ALLOWED_EXTRA`` specifically
  for this). Never a lead name/email/phone or any other PII (``payload`` is
  not logged).

Every SR-21 emit call site (lead_captured, lead_assigned,
lead_auto_assigned, lead_stage_transitioned, lead_converted,
conversation_escalated, ingestion_failed) goes through this function, never
``events_repository.emit_event`` directly, so the fail-open guarantee cannot
be forgotten at any one call site (mirrors
``api.leads.assignment.assign_lead_fail_open``'s SR-20 D3 precedent).
"""
from __future__ import annotations

from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger, log_context

from api.notifications.events_repository import emit_event

_log = get_logger(__name__)


async def emit_event_safe(
    db: Database,
    claims: AuthClaims,
    *,
    kind: str,
    category: str,
    target_type: str | None,
    target_id: str | None,
    payload: dict[str, Any] | None,
    actor_id: str | None,
) -> str | None:
    """Emit one feed event; never raises. Returns the ``event_id``, or
    ``None`` if the write failed for any reason.

    Callers MUST invoke this only after their own mutation has already been
    durably committed (D2's ordering) -- this function never creates or
    rolls back the caller's write, and never blocks or reverses it.
    """
    try:
        return await emit_event(
            db,
            claims,
            kind=kind,
            category=category,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            actor_id=actor_id,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-open by design (D2)
        # log_context (not just extra=) so tenant_id -- a ContextVar-sourced
        # field, per common.logging -- is present even when this runs
        # outside an active request's own bound context. event/kind ride
        # extra=, both are in _ALLOWED_EXTRA. Never payload/PII.
        with log_context(tenant_id=claims.tenant_id):
            _log.warning(
                "notification event emit failed; feed item will be missing",
                extra={
                    "event": "notification_event_emit_failed",
                    "kind": kind,
                    "reason": str(exc),
                },
            )
        return None
