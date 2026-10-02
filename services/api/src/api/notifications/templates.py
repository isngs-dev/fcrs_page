"""Plain-text notification message builders (S9.2, Scope §1).

Pure, no I/O -- each function returns ``(subject, body)``. No templating
engine, no branding, no HTML (deferred to a later sprint per S9.2's "isn't"
list). Times are rendered in the event's stored IANA ``timezone`` via
``zoneinfo`` -- times are stored UTC, displayed local.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# Offsets are human-readable labels used only in the message text.
_OFFSET_LABELS: dict[str, str] = {
    "3d": "3 days",
    "24h": "24 hours",
    "1h": "1 hour",
}


def _local_wall_clock(starts_at: datetime, timezone: str) -> str:
    """Render ``starts_at`` (any tz-aware datetime) in the local ``timezone``."""
    local = starts_at.astimezone(ZoneInfo(timezone))
    return local.strftime("%Y-%m-%d %H:%M %Z")


def booking_confirmation_message(
    *,
    starts_at: datetime,
    timezone: str,
    calendly_link: str | None = None,
    meet_url: str | None = None,
    rescheduled_from: datetime | None = None,
) -> tuple[str, str]:
    """Build the ``(subject, body)`` for a booking-confirmation email.

    ``rescheduled_from``: the old slot's start when this booking replaces an
    earlier one -- the email then says so.

    ``calendly_link``, when the tenant has one configured, replaces the
    generic "contact us" fallback line with a direct reschedule/manage link.
    ``meet_url`` (SR-22), when the calendar sync created a Google Meet
    conference, adds a join-link line -- omitted entirely (not a placeholder
    line) when there isn't one, so the email never implies a call method
    that doesn't exist.
    """
    when = _local_wall_clock(starts_at, timezone)
    subject = "Your call has been rescheduled" if rescheduled_from else "Your call is confirmed"
    moved_line = (
        f"\nThis replaces your earlier booking on {_local_wall_clock(rescheduled_from, timezone)}."
        if rescheduled_from
        else ""
    )
    reschedule_line = (
        f"Need to reschedule? Manage your booking here: {calendly_link}"
        if calendly_link
        else "If you need to reschedule, please contact us."
    )
    meet_line = f"\n\nJoin the call: {meet_url}" if meet_url else ""
    body = (
        "Your call is confirmed.\n\n"
        f"When: {when}"
        f"{moved_line}"
        f"{meet_line}\n\n"
        f"{reschedule_line}"
    )
    return subject, body


def rep_booking_notification_message(
    *,
    starts_at: datetime,
    timezone: str,
    visitor_name: str | None,
    visitor_email: str | None,
    visitor_phone: str | None,
    meet_url: str | None = None,
    rescheduled_from: datetime | None = None,
) -> tuple[str, str]:
    """Build the ``(subject, body)`` telling the calendar owner (the rep) a
    visitor booked a call. Same omit-if-absent rule for every optional line.
    """
    when = _local_wall_clock(starts_at, timezone)
    who = visitor_name or visitor_email or "A website visitor"
    if rescheduled_from:
        subject = f"Call rescheduled: {who}"
        lines = [
            f"{who} rescheduled their call with you.", "", f"When: {when}",
            f"Previously: {_local_wall_clock(rescheduled_from, timezone)}",
        ]
    else:
        subject = f"New call booked: {who}"
        lines = [f"{who} booked a call with you.", "", f"When: {when}"]
    if visitor_name:
        lines.append(f"Name: {visitor_name}")
    if visitor_email:
        lines.append(f"Email: {visitor_email}")
    if visitor_phone:
        lines.append(f"Phone: {visitor_phone}")
    if meet_url:
        lines += ["", f"Join the call: {meet_url}"]
    return subject, "\n".join(lines)


def reminder_message(
    *, offset: str, starts_at: datetime, timezone: str, meet_url: str | None = None
) -> tuple[str, str]:
    """Build the ``(subject, body)`` for a reminder email at ``offset`` before the call.

    ``meet_url`` (SR-22): same omit-if-absent rule as
    ``booking_confirmation_message`` -- a reminder with nowhere to click
    would be worse than one that just states the time.
    """
    when = _local_wall_clock(starts_at, timezone)
    label = _OFFSET_LABELS.get(offset, offset)
    subject = f"Reminder: your call is in {label} ({offset})"
    meet_line = f"\n\nJoin the call: {meet_url}" if meet_url else ""
    body = (
        f"This is a reminder that your call is coming up in {label}.\n\n"
        f"When: {when}"
        f"{meet_line}"
    )
    return subject, body


def password_reset_message(*, reset_url: str) -> tuple[str, str]:
    """Build the ``(subject, body)`` for a password-reset email.

    The raw token lives ONLY in ``reset_url`` -- the caller must never pass
    the raw token anywhere else (dedupe_key/payload/logs must carry only its
    hash, per S9.2 decision 1/4).
    """
    subject = "Reset your password"
    body = (
        "We received a request to reset your password.\n\n"
        f"Click the link below to choose a new password:\n{reset_url}\n\n"
        "If you did not request this, you can safely ignore this email."
    )
    return subject, body
