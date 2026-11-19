"""Booking into the connected calendar, shared by the phone and email agents and the
"manage your booking" page (plan 1.6).

The widget's booking route (``routes.py``) books inline (lead linking, voice call), but its
confirmation email and reschedule clean-up use ``send_confirmation`` / ``retire_booking``
too, so every channel confirms and retires a booking the same way.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from common.auth import AuthClaims
from common.db import Database
from common.errors import AppException
from common.logging import get_logger

from api.config import get_api_settings
from api.notifications.repository import enqueue_notification
from api.notifications.tasks import send_notification
from api.notifications.templates import (
    booking_confirmation_message,
    rep_booking_notification_message,
)
from api.scheduling import manage
from api.scheduling.calendar import CalendarEvent, CalendarRef, calendar_provider_for_async
from api.scheduling.calendar_config_repository import CalendarConfig, get_calendar_config
from api.scheduling.reminder_repository import create_reminder_jobs, skip_pending_reminders
from api.scheduling.repository import (
    ScheduleEvent,
    cancel_event,
    create_event,
    delete_event,
    get_availability,
    get_event,
    list_booked,
    update_event_calendar_ref,
)
from api.scheduling.slots import compute_slots

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


async def next_open_slots(db: Database, claims: AuthClaims, now: datetime,
                          count: int | None = 2, ignore_bookings: bool = False,
                          on: datetime | None = None) -> tuple[list[datetime], str] | None:
    """The next open native slots (>= 2 h ahead, within 7 days, or on the day of ``on``;
    ``count=None`` = all), or ``None`` when the tenant books through Calendly or has no
    availability. A connected calendar (Google) only removes its busy times, like the widget
    (S8.2 decision 3). ``ignore_bookings`` = business hours only (tells "already booked" from
    "closed")."""
    calendar = await get_calendar_config(db, claims)
    if calendar is not None and calendar.enabled and calendar.provider == "calendly":
        return None
    availability = await get_availability(db, claims)
    if availability is None:
        return None
    zone = ZoneInfo(availability.timezone)
    start_day: date = (on or now).astimezone(zone).date()
    end_day = start_day if on else start_day + timedelta(days=7)
    window_start = datetime.combine(start_day, dtime(0), tzinfo=zone).astimezone(UTC)
    window_end = window_start + timedelta(days=8)
    booked = ([] if ignore_bookings else
              await list_booked(db, claims, window_start=window_start, window_end=window_end))
    if not ignore_bookings:
        booked += await busy_times(db, claims, window_start, window_end)
    slots = compute_slots(availability.rules, availability.timezone, start_day, end_day, booked,
                          now=now + timedelta(hours=2), window_max_days=8)
    return [s.starts_at for s in slots[:count]], availability.timezone


async def book_slot(db: Database, claims: AuthClaims, *, start: datetime, timezone: str,
                    email: str | None, name: str | None, lead_id: str | None,
                    visitor_id: str | None, attendee_phone: str | None,
                    consent: dict[str, Any]
                    ) -> tuple[str, ScheduleEvent | None, str | None, str | None]:
    """Book an open native slot -> ``(status, event, meet_url, organizer)``. ``"taken"`` when
    it went meanwhile (or never was open), ``"failed"`` when the connected calendar refused it
    (the booking is undone: never a booked row without its calendar event). Reminders are
    created with the booking."""
    found = await next_open_slots(db, claims, datetime.now(UTC), count=None, on=start)
    if not found or start not in found[0]:
        return "taken", None, None, None
    availability = await get_availability(db, claims)
    minutes = int((availability.rules if availability else {}).get("slot_minutes", 30))
    try:
        event = await create_event(
            db, claims, starts_at=start, ends_at=start + timedelta(minutes=minutes),
            timezone=timezone, visitor_id=visitor_id, lead_id=lead_id, email=email, name=name,
            consent={**consent, "captured_at": datetime.now(UTC).isoformat()})
    except AppException:  # SLOT_UNAVAILABLE race
        return "taken", None, None, None
    await create_reminder_jobs(db, claims, event_id=event.event_id, starts_at=event.starts_at,
                               now=datetime.now(UTC))
    try:
        meet_url, organizer = await sync_to_calendar(
            db, claims, event, attendee_email=email, attendee_name=name,
            attendee_phone=attendee_phone)
    except CalendarSyncFailed:
        return "failed", None, None, None
    return "booked", event, meet_url, organizer


async def retire_booking(db: Database, claims: AuthClaims, event_id: str) -> bool:
    """Cancel a booking: marked cancelled, its unsent reminders skipped, its calendar event
    deleted (best effort, logged). Returns whether it was still booked (idempotent)."""
    event = await get_event(db, claims, event_id)
    cancelled = await cancel_event(db, claims, event_id)
    if not cancelled:
        return False
    await skip_pending_reminders(db, claims, event_id)
    calendar = await get_calendar_config(db, claims)
    if event is not None and event.calendar_ref and calendar is not None and calendar.enabled:
        name, _, external_id = event.calendar_ref.partition(":")
        try:
            provider = await calendar_provider(calendar)
            await provider.delete_event(claims, CalendarRef(provider=name,
                                                            external_id=external_id))
        except Exception as exc:  # noqa: BLE001 -- the booking is cancelled; say the calendar wasn't
            _log.warning("calendar event not deleted", extra={
                "event": "booking_calendar_delete_failed", "reason": type(exc).__name__})
    return True


async def confirmation_extras(db: Database, claims: AuthClaims, *, contact_id: str | None,
                              lead_id: str | None, email: str | None
                              ) -> tuple[list[str], str | None]:
    """Plan 1.6: the agenda (their own words) and one question the team still needs. Never
    raises: a lookup failure leaves the extras out (logged) -- the confirmation still goes."""
    try:
        return await _extras(db, claims, contact_id=contact_id, lead_id=lead_id, email=email)
    except Exception as exc:  # noqa: BLE001 -- optional content; said in the log
        _log.warning("booking confirmation extras skipped", extra={
            "event": "booking_confirm_extras_failed", "reason": type(exc).__name__})
        return [], None


async def _extras(db: Database, claims: AuthClaims, *, contact_id: str | None,
                  lead_id: str | None, email: str | None) -> tuple[list[str], str | None]:
    from api.agents import memory  # noqa: PLC0415 -- agents import scheduling
    from api.contacts.repository import (  # noqa: PLC0415
        get_contact_by_lead_id,
        get_contact_id_by_identity,
    )
    from api.qualification import repository as qualification  # noqa: PLC0415

    if contact_id is None and lead_id:
        contact = await get_contact_by_lead_id(db, claims, lead_id)
        contact_id = contact.contact_id if contact else None
    if contact_id is None and email:
        contact_id = await get_contact_id_by_identity(db, claims, identity_type="email",
                                                      identity_value=email)
    if contact_id is None:
        return [], None
    agenda = memory.agenda(await memory.items(db, claims, contact_id))
    config = await qualification.get_config(db, claims)
    known = (await qualification.get_record(db, claims, contact_id)).answers
    prepare = next((q.ask for q in config.questions if q.key not in known), None)
    return agenda, prepare


async def send_confirmation(db: Database, claims: AuthClaims, event: ScheduleEvent, *,
                            recipient: str, name: str | None, business: str | None,
                            meet_url: str | None, organizer: str | None,
                            rescheduled_from: datetime | None = None,
                            calendly_link: str | None = None, lead_id: str | None = None,
                            contact_id: str | None = None,
                            copies: list[str] | None = None,
                            resend: str | None = None) -> None:
    """The booking-confirmation email, once per booking (and once per ``copies`` address --
    "my other mail as well", 2026-11-18): agenda, prep question and the manage link (plan
    1.6). ``resend``: sent again on request ("send me the link again", 2026-11-19), once per
    this key. Raises on a failed enqueue -- callers decide (a booking never fails because of
    its email)."""
    agenda, prepare = await confirmation_extras(db, claims, contact_id=contact_id,
                                                lead_id=lead_id or event.lead_id,
                                                email=recipient)
    subject, body = booking_confirmation_message(
        starts_at=event.starts_at, timezone=event.timezone, calendly_link=calendly_link,
        meet_url=meet_url, rescheduled_from=rescheduled_from, business_name=business,
        visitor_name=name, agenda=agenda, prepare=prepare,
        manage_url=manage.manage_url(str(claims.tenant_id), event.event_id))
    payload: dict[str, Any] = {"kind": "booking_confirm", "event_id": event.event_id}
    if business:
        payload["from_name"] = business
    if organizer:
        payload["reply_to"] = organizer
    for to in [recipient, *[c for c in (copies or []) if c and c != recipient]]:
        dedupe = (f"booking_confirm:{event.event_id}" + (f":resend:{resend}" if resend else "")
                  + ("" if to == recipient else f":{to}"))
        job_id = await enqueue_notification(
            db, claims, channel="email", recipient=to, subject=subject, body=body,
            dedupe_key=dedupe, payload=payload, lead_id=lead_id or event.lead_id)
        if job_id is not None:
            send_notification.delay(job_id=job_id, tenant_id=claims.tenant_id,
                                    correlation_id="")


async def notify_organizer(db: Database, claims: AuthClaims, event: ScheduleEvent,
                           organizer: str, *, name: str | None, email: str | None,
                           phone: str | None, meet_url: str | None,
                           rescheduled_from: datetime | None = None) -> None:
    """The "new call booked" email to the calendar's owner, once per booking. Best effort:
    the calendar event already shows the call."""
    try:
        subject, body = rep_booking_notification_message(
            starts_at=event.starts_at, timezone=event.timezone, visitor_name=name,
            visitor_email=email, visitor_phone=phone, meet_url=meet_url,
            rescheduled_from=rescheduled_from)
        job_id = await enqueue_notification(
            db, claims, channel="email", recipient=organizer, subject=subject, body=body,
            dedupe_key=f"booking_rep_notify:{event.event_id}",
            payload={"kind": "booking_rep_notify", "event_id": event.event_id})
        if job_id is not None:
            send_notification.delay(job_id=job_id, tenant_id=claims.tenant_id, correlation_id="")
    except Exception as exc:  # noqa: BLE001 -- logged, never fails the booking
        _log.warning("booking owner notice not sent", extra={
            "event": "booking_rep_notify_failed", "reason": type(exc).__name__})
