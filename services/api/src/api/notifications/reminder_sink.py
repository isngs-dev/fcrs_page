"""``NotificationReminderSink`` -- a ``ReminderSink`` backed by S9.1 notifications (S9.2, Scope §3).

Implements S8.3's ``ReminderSink.dispatch`` protocol: resolves the event's
recipient (``api.notifications.recipients.resolve_event_recipient``), builds
the reminder message (``api.notifications.templates.reminder_message``),
enqueues through S9.1's idempotent front door
(``api.notifications.repository.enqueue_notification``), and ``.delay()``s
``notifications.send_notification`` only for a genuinely new job. No
recipient -> raises ``NoRecipientError`` (a ``ValidationError``) BEFORE any
enqueue -- ``api.scheduling.tasks._execute_send`` maps this to a ``skipped``
reminder job, not ``failed`` (S9.2 Decision 2/3).

Imported lazily (function-local) from ``api.scheduling.reminders
.reminder_sink_for`` to avoid a module-load-time cycle between the
scheduling and notifications packages -- do NOT import this module eagerly
from ``api.scheduling.reminders``.

SR-9.3 D4/Scope §3: passes ``lead_id=contact.lead_id`` (from the
``get_event_contact`` call this sink already performs at line ~52) onto
``enqueue_notification`` -- zero extra queries -- so a reminder is
attributable on the customer-360 timeline. ``contact.lead_id`` is honestly
``None`` for a Calendly-sourced event (never has a lead) or an event whose
autolink degraded; that is passed through as-is, never fabricated.
"""
from __future__ import annotations

from common.auth import AuthClaims
from common.db import Database
from common.logging import _correlation_id

from api.notifications.recipients import NoRecipientError, resolve_event_recipient
from api.notifications.repository import (
    enqueue_notification,
    get_notification_job_id_by_dedupe_key,
)
from api.notifications.tasks import send_notification
from api.notifications.templates import reminder_message
from api.scheduling.reminders import DispatchRef, ReminderDispatch
from api.scheduling.repository import get_event_contact


class NotificationReminderSink:
    """``ReminderSink`` implementation that enqueues a real notification job."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def dispatch(self, claims: AuthClaims, reminder: ReminderDispatch) -> DispatchRef:
        recipient = await resolve_event_recipient(self._db, claims, reminder.event_id)
        if recipient is None:
            raise NoRecipientError(
                "No recipient could be resolved for this reminder.",
                code="NO_RECIPIENT",
            )

        # ReminderDispatch itself carries no timezone (S8.3 -- a deliberately
        # non-PII payload); re-read the event's stored IANA timezone so the
        # reminder body renders local wall-clock time, per Scope §1.
        contact = await get_event_contact(self._db, claims, reminder.event_id)
        timezone = contact.timezone if contact is not None else "UTC"

        subject, body = reminder_message(
            offset=reminder.offset, starts_at=reminder.starts_at, timezone=timezone
        )

        dedupe_key = f"reminder:{reminder.event_id}:{reminder.offset}"

        job_id = await enqueue_notification(
            self._db,
            claims,
            channel="email",
            recipient=recipient,
            subject=subject,
            body=body,
            dedupe_key=dedupe_key,
            payload={"kind": "reminder", "event_id": reminder.event_id, "offset": reminder.offset},
            lead_id=contact.lead_id if contact is not None else None,
        )

        if job_id is not None:
            correlation_id = _correlation_id.get() or ""
            send_notification.delay(
                job_id=job_id,
                tenant_id=claims.tenant_id,
                correlation_id=correlation_id,
            )
            return DispatchRef(sink="notification", ref=job_id)

        existing = await get_notification_job_id_by_dedupe_key(self._db, claims, dedupe_key)
        return DispatchRef(sink="notification", ref=existing or "")
