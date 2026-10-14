"""Unit tests for api.notifications.reminder_sink.NotificationReminderSink (S9.2, Scope §3).

Covers:
- dispatch on a resolvable event -> enqueue_notification called with
  dedupe_key="reminder:<event_id>:<offset>" + channel="email"; .delay()
  called once for a genuinely new job; returns DispatchRef(sink="notification").
- A conflict (enqueue -> None) -> .delay() NOT called, returns the EXISTING
  job id (looked up via get_notification_job_id_by_dedupe_key).
- No recipient -> raises NoRecipientError, does NOT enqueue.
- The enqueue body comes from templates.reminder_message.
- reminder_sink_for("notification", db=<stub>) returns a NotificationReminderSink;
  reminder_sink_for("log") still returns LogReminderSink (db ignored);
  unknown -> ReminderSinkConfigError.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from common.auth import AuthClaims, Role

from api.notifications.reminder_sink import NotificationReminderSink
from api.scheduling.reminders import DispatchRef, ReminderDispatch
from api.scheduling.repository import EventContact

_TENANT_ID = "tenant-reminder-sink-test"
_EVENT_ID = "event-reminder-sink-test"
_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def _claims() -> AuthClaims:
    return AuthClaims(subject="system:scheduling", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)


def _dispatch(offset: str = "1h") -> ReminderDispatch:
    return ReminderDispatch(event_id=_EVENT_ID, offset=offset, run_at=_NOW, starts_at=_NOW)


def _contact() -> EventContact:
    return EventContact(
        lead_id=None, visitor_id="visitor-1", timezone="UTC", starts_at=_NOW, status="booked"
    )


class _StubDatabase:
    """Answers the business-name lookup (tenants) only; nothing else is read here."""

    def __init__(self, tenant_name: str | None = None) -> None:
        self._tenant_name = tenant_name

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "FROM tenants" in query and self._tenant_name:
            return {"id": args[0], "name": self._tenant_name}
        return None

    async def close(self) -> None:
        pass


async def test_dispatch_resolvable_event_enqueues_and_delays_new_job() -> None:
    sink = NotificationReminderSink(_StubDatabase())  # type: ignore[arg-type]

    with (
        patch(
            "api.notifications.reminder_sink.resolve_event_recipient",
            AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.notifications.reminder_sink.get_event_contact", AsyncMock(return_value=_contact())
        ),
        patch(
            "api.notifications.reminder_sink.enqueue_notification",
            AsyncMock(return_value="job-new-1"),
        ) as mock_enqueue,
        patch("api.notifications.reminder_sink.send_notification") as mock_task,
    ):
        ref = await sink.dispatch(_claims(), _dispatch(offset="1h"))

    assert ref == DispatchRef(sink="notification", ref="job-new-1")
    mock_enqueue.assert_awaited_once()
    _, kwargs = mock_enqueue.call_args
    assert kwargs["channel"] == "email"
    assert kwargs["recipient"] == "lead@example.com"
    assert kwargs["dedupe_key"] == f"reminder:{_EVENT_ID}:1h"
    mock_task.delay.assert_called_once()
    _, delay_kwargs = mock_task.delay.call_args
    assert delay_kwargs["job_id"] == "job-new-1"
    assert delay_kwargs["tenant_id"] == _TENANT_ID


async def test_dispatch_conflict_does_not_delay_returns_existing_job_id() -> None:
    sink = NotificationReminderSink(_StubDatabase())  # type: ignore[arg-type]

    with (
        patch(
            "api.notifications.reminder_sink.resolve_event_recipient",
            AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.notifications.reminder_sink.get_event_contact", AsyncMock(return_value=_contact())
        ),
        patch(
            "api.notifications.reminder_sink.enqueue_notification", AsyncMock(return_value=None)
        ),
        patch(
            "api.notifications.reminder_sink.get_notification_job_id_by_dedupe_key",
            AsyncMock(return_value="job-existing-1"),
        ),
        patch("api.notifications.reminder_sink.send_notification") as mock_task,
    ):
        ref = await sink.dispatch(_claims(), _dispatch(offset="24h"))

    assert ref == DispatchRef(sink="notification", ref="job-existing-1")
    mock_task.delay.assert_not_called()


async def test_dispatch_no_recipient_raises_and_does_not_enqueue() -> None:
    from api.notifications.recipients import NoRecipientError

    sink = NotificationReminderSink(_StubDatabase())  # type: ignore[arg-type]

    with (
        patch(
            "api.notifications.reminder_sink.resolve_event_recipient", AsyncMock(return_value=None)
        ),
        patch("api.notifications.reminder_sink.enqueue_notification", AsyncMock()) as mock_enqueue,
        patch("api.notifications.reminder_sink.send_notification") as mock_task,
    ):
        try:
            await sink.dispatch(_claims(), _dispatch(offset="1h"))
            raised = False
        except NoRecipientError:
            raised = True

    assert raised
    mock_enqueue.assert_not_called()
    mock_task.delay.assert_not_called()


async def test_dispatch_body_comes_from_reminder_message_template() -> None:
    sink = NotificationReminderSink(_StubDatabase())  # type: ignore[arg-type]

    with (
        patch(
            "api.notifications.reminder_sink.resolve_event_recipient",
            AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.notifications.reminder_sink.get_event_contact", AsyncMock(return_value=_contact())
        ),
        patch(
            "api.notifications.reminder_sink.enqueue_notification",
            AsyncMock(return_value="job-1"),
        ) as mock_enqueue,
        patch("api.notifications.reminder_sink.send_notification"),
        patch(
            "api.notifications.reminder_sink.reminder_message",
            return_value=("stub subject", "stub body"),
        ) as mock_template,
    ):
        await sink.dispatch(_claims(), _dispatch(offset="1h"))

    mock_template.assert_called_once()
    _, kwargs = mock_enqueue.call_args
    assert kwargs["subject"] == "stub subject"
    assert kwargs["body"] == "stub body"


# ==============================================================================
# reminder_sink_for -- selection (S9.2, Scope §4)
# ==============================================================================


def test_reminder_sink_for_notification_returns_notification_sink() -> None:
    from api.scheduling.reminders import reminder_sink_for

    sink = reminder_sink_for("notification", db=_StubDatabase())  # type: ignore[arg-type]
    assert isinstance(sink, NotificationReminderSink)


def test_reminder_sink_for_log_ignores_db_returns_log_sink() -> None:
    from api.scheduling.reminders import LogReminderSink, reminder_sink_for

    sink = reminder_sink_for("log", db=_StubDatabase())  # type: ignore[arg-type]
    assert isinstance(sink, LogReminderSink)

    sink_no_db = reminder_sink_for("log")
    assert isinstance(sink_no_db, LogReminderSink)


def test_reminder_sink_for_unknown_raises_config_error() -> None:
    from api.scheduling.reminders import ReminderSinkConfigError, reminder_sink_for

    try:
        reminder_sink_for("carrier-pigeon")
        raised = False
    except ReminderSinkConfigError:
        raised = True
    assert raised


async def test_dispatch_brands_the_reminder_with_the_business_and_visitor_name() -> None:
    """Reminders landed in spam like the old confirmation: generic text from
    a shared 'Bookings' sender. Same branding as the booking confirmation."""
    sink = NotificationReminderSink(_StubDatabase(tenant_name="Acme Roofing"))  # type: ignore[arg-type]
    contact = EventContact(
        lead_id=None, visitor_id="visitor-1", timezone="UTC", starts_at=_NOW, status="booked",
        name="Jane Smith",
    )
    with (
        patch(
            "api.notifications.reminder_sink.resolve_event_recipient",
            AsyncMock(return_value="jane@example.com"),
        ),
        patch("api.notifications.reminder_sink.get_event_contact", AsyncMock(return_value=contact)),
        patch(
            "api.notifications.reminder_sink.enqueue_notification",
            AsyncMock(return_value="job-1"),
        ) as mock_enqueue,
        patch("api.notifications.reminder_sink.send_notification"),
    ):
        await sink.dispatch(_claims(), _dispatch(offset="24h"))

    kwargs = mock_enqueue.call_args.kwargs
    assert kwargs["subject"] == "Reminder: your call with Acme Roofing is in 24 hours"
    assert kwargs["body"].startswith("Hi Jane,")
    assert kwargs["payload"]["from_name"] == "Acme Roofing"
