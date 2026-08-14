"""Unit tests for api.notifications.templates (S9.2, Scope §1).

Pure plain-text message builders -- no I/O. Covers:
- Each builder returns a non-empty (subject, body) tuple.
- password_reset_message's body contains the reset_url (and thus the token).
- reminder_message/booking_confirmation_message render the local wall-clock
  time for the given IANA timezone from a known UTC starts_at.
- No PII beyond what is intended (no stray tenant/visitor ids).
"""
from __future__ import annotations

from datetime import UTC, datetime

from api.notifications.templates import (
    booking_confirmation_message,
    password_reset_message,
    reminder_message,
)

# 2026-07-20T14:00:00Z -> 2026-07-20 10:00 local in America/New_York (EDT, UTC-4).
_STARTS_AT_UTC = datetime(2026, 7, 20, 14, 0, 0, tzinfo=UTC)
_TZ = "America/New_York"


def test_booking_confirmation_message_non_empty_subject_and_body() -> None:
    subject, body = booking_confirmation_message(starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert subject
    assert body


def test_booking_confirmation_message_renders_local_time() -> None:
    _subject, body = booking_confirmation_message(starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert "10:00" in body
    assert "2026-07-20" in body


def test_reminder_message_non_empty_subject_and_body() -> None:
    subject, body = reminder_message(offset="1h", starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert subject
    assert body


def test_reminder_message_renders_local_time() -> None:
    _subject, body = reminder_message(offset="24h", starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert "10:00" in body
    assert "2026-07-20" in body


def test_reminder_message_mentions_offset() -> None:
    subject, body = reminder_message(offset="3d", starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert "3d" in subject or "3d" in body


def test_reminder_message_different_timezone_renders_different_local_time() -> None:
    _subject, body_ny = reminder_message(offset="1h", starts_at=_STARTS_AT_UTC, timezone="America/New_York")
    _subject2, body_utc = reminder_message(offset="1h", starts_at=_STARTS_AT_UTC, timezone="UTC")
    assert body_ny != body_utc
    assert "14:00" in body_utc


def test_password_reset_message_body_contains_reset_url() -> None:
    reset_url = "http://localhost:3000/reset-password?token=super-secret-raw-token-value"
    subject, body = password_reset_message(reset_url=reset_url)
    assert subject
    assert reset_url in body


def test_password_reset_message_is_pure_same_input_same_output() -> None:
    reset_url = "http://localhost:3000/reset-password?token=abc123"
    result1 = password_reset_message(reset_url=reset_url)
    result2 = password_reset_message(reset_url=reset_url)
    assert result1 == result2


def test_booking_confirmation_message_no_stray_pii_fields() -> None:
    subject, body = booking_confirmation_message(starts_at=_STARTS_AT_UTC, timezone=_TZ)
    combined = subject + body
    assert "tenant_id" not in combined
    assert "visitor_id" not in combined


def test_booking_confirmation_message_includes_calendly_link_when_given() -> None:
    link = "https://calendly.com/acme/intro"
    _subject, body = booking_confirmation_message(
        starts_at=_STARTS_AT_UTC, timezone=_TZ, calendly_link=link
    )
    assert link in body


def test_booking_confirmation_message_no_calendly_link_falls_back_to_contact_us() -> None:
    _subject, body = booking_confirmation_message(starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert "contact us" in body
    assert "calendly.com" not in body


def test_booking_confirmation_message_includes_meet_url_when_given() -> None:
    join_link = "https://meet.google.com/abc-defg-hij"
    _subject, body = booking_confirmation_message(
        starts_at=_STARTS_AT_UTC, timezone=_TZ, meet_url=join_link
    )
    assert join_link in body
    assert "Join the call:" in body


def test_booking_confirmation_message_no_meet_url_omits_join_line() -> None:
    _subject, body = booking_confirmation_message(starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert "Join the call:" not in body
    assert "meet.google.com" not in body


def test_reminder_message_includes_meet_url_when_given() -> None:
    join_link = "https://meet.google.com/abc-defg-hij"
    _subject, body = reminder_message(
        offset="1h", starts_at=_STARTS_AT_UTC, timezone=_TZ, meet_url=join_link
    )
    assert join_link in body
    assert "Join the call:" in body


def test_reminder_message_no_meet_url_omits_join_line() -> None:
    _subject, body = reminder_message(offset="1h", starts_at=_STARTS_AT_UTC, timezone=_TZ)
    assert "Join the call:" not in body
    assert "meet.google.com" not in body
