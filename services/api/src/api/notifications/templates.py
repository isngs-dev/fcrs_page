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
    *, starts_at: datetime, timezone: str, calendly_link: str | None = None
) -> tuple[str, str]:
    """Build the ``(subject, body)`` for a booking-confirmation email.

    ``calendly_link``, when the tenant has one configured, replaces the
    generic "contact us" fallback line with a direct reschedule/manage link.
    """
    when = _local_wall_clock(starts_at, timezone)
    subject = "Your call is confirmed"
    reschedule_line = (
        f"Need to reschedule? Manage your booking here: {calendly_link}"
        if calendly_link
        else "If you need to reschedule, please contact us."
    )
    body = (
        "Your call is confirmed.\n\n"
        f"When: {when}\n\n"
        f"{reschedule_line}"
    )
    return subject, body


def reminder_message(*, offset: str, starts_at: datetime, timezone: str) -> tuple[str, str]:
    """Build the ``(subject, body)`` for a reminder email at ``offset`` before the call."""
    when = _local_wall_clock(starts_at, timezone)
    label = _OFFSET_LABELS.get(offset, offset)
    subject = f"Reminder: your call is in {label} ({offset})"
    body = (
        f"This is a reminder that your call is coming up in {label}.\n\n"
        f"When: {when}"
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
