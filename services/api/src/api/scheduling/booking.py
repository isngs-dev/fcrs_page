"""Booking into the connected calendar, shared by the phone and email agents.

The widget's booking route (``routes.py``) does the same steps inline. These are the parts
the agents need: the calendar's busy times, the calendar event (and Meet link) for a booked
slot, and the "new call booked" email to the calendar's owner.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger

from api.config import get_api_settings
from api.notifications.repository import enqueue_notification
from api.notifications.tasks import send_notification
from api.notifications.templates import rep_booking_notification_message
from api.scheduling.calendar import CalendarEvent, calendar_provider_for_async
from api.scheduling.calendar_config_repository import CalendarConfig, get_calendar_config
from api.scheduling.repository import ScheduleEvent, delete_event, update_event_calendar_ref

_log = get_logger(__name__)


class CalendarSyncFailed(Exception):  # noqa: N818 -- reads as what happened
    """The connected calendar refused the event. The booking was undone."""


async def calendar_provider(calendar: CalendarConfig) -> Any:
    settings = get_api_settings()
    return await calendar_provider_for_async(
        calendar, timeout_seconds=settings.calendar_http_timeout_seconds,
        google_client_id=settings.google_oauth_client_id,
        google_client_secret=settings.google_oauth_client_secret)


async def busy_times(db: Database, claims: AuthClaims, start: datetime, end: datetime
                     ) -> list[tuple[datetime, datetime]]:
    """The connected calendar's busy times in ``[start, end)``. Best effort, like the widget
    (S8.2 decision 3): an unreachable calendar adds nothing, and the sync at booking decides."""
    calendar = await get_calendar_config(db, claims)
    if calendar is None or not calendar.enabled:
        return []
    try:
        provider = await calendar_provider(calendar)
        busy = await provider.free_busy(claims, (start, end))
    except Exception:  # noqa: BLE001 -- logged; native slots still apply
        _log.warning("free-busy degraded to native slots", extra={
            "event": "calendar_freebusy_degraded", "provider": calendar.provider})
        return []
    return [(b.start, b.end) for b in busy]


async def sync_to_calendar(db: Database, claims: AuthClaims, event: ScheduleEvent, *,
                           attendee_email: str | None, attendee_name: str | None,
                           attendee_phone: str | None) -> tuple[str | None, str | None]:
    """Put a just-booked event in the connected calendar -> ``(meet_url, organizer_email)``;
    ``(None, None)`` when no calendar is connected. A refusal deletes the booking (its
    reminders cascade) and raises ``CalendarSyncFailed``: never a booked row without its
    calendar event (S8.2 decision 4)."""
    calendar = await get_calendar_config(db, claims)
    if calendar is None or not calendar.enabled:
        return None, None
    try:
        provider = await calendar_provider(calendar)
        ref = await provider.create_event(claims, CalendarEvent(
            event_id=event.event_id, starts_at=event.starts_at, ends_at=event.ends_at,
            timezone=event.timezone, attendee_email=attendee_email, attendee_name=attendee_name,
            attendee_phone=attendee_phone))
    except Exception as exc:
        await delete_event(db, claims, event.event_id)
        _log.warning("booking calendar sync failed", extra={
            "event": "calendar_sync_failed", "provider": calendar.provider,
            "reason": type(exc).__name__})
        raise CalendarSyncFailed from exc
    await update_event_calendar_ref(db, claims, event.event_id,
                                    f"{ref.provider}:{ref.external_id}", ref.meet_url)
    return ref.meet_url, ref.organizer_email


async def notify_organizer(db: Database, claims: AuthClaims, event: ScheduleEvent,
                           organizer: str, *, name: str | None, email: str | None,
                           phone: str | None, meet_url: str | None) -> None:
    """The "new call booked" email to the calendar's owner, once per booking. Best effort:
    the calendar event already shows the call."""
    try:
        subject, body = rep_booking_notification_message(
            starts_at=event.starts_at, timezone=event.timezone, visitor_name=name,
            visitor_email=email, visitor_phone=phone, meet_url=meet_url)
        job_id = await enqueue_notification(
            db, claims, channel="email", recipient=organizer, subject=subject, body=body,
            dedupe_key=f"booking_rep_notify:{event.event_id}",
            payload={"kind": "booking_rep_notify", "event_id": event.event_id})
        if job_id is not None:
            send_notification.delay(job_id=job_id, tenant_id=claims.tenant_id, correlation_id="")
    except Exception as exc:  # noqa: BLE001 -- logged, never fails the booking
        _log.warning("booking owner notice not sent", extra={
            "event": "booking_rep_notify_failed", "reason": type(exc).__name__})
