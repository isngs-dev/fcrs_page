"""A10 intake parsing -- pure functions: notification e-mail fields, form payload
normalisation, consent answers, Gmail forwarding confirmation, timezone inference."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.intake import parsers

_META = """You have a new lead!

Form: Spring roof check-ups
Full name: Jane Doe
Email: Jane.Doe@Example.com
Phone number: (202) 555-0123
What service do you need?: Flat roof repair
I agree to be contacted by phone, including by an AI assistant: Yes

View this lead in Meta Business Suite.
"""


def test_label_value_lines_are_read() -> None:
    fields = parsers.label_values(_META)
    assert fields["Full name"] == "Jane Doe"
    assert fields["Phone number"] == "(202) 555-0123"
    assert fields["What service do you need?"] == "Flat roof repair"
    assert "View this lead in Meta Business Suite." not in fields


def test_label_on_one_line_value_on_the_next() -> None:
    fields = parsers.label_values("Full name:\nJane Doe\n\nEmail:\njane@example.com\n")
    assert fields == {"Full name": "Jane Doe", "Email": "jane@example.com"}


def test_html_notification_is_flattened() -> None:
    html = ("<table><tr><td>Full name:</td><td>Jane Doe</td></tr>"
            "<tr><td>Email:</td><td><a href='mailto:jane@example.com'>jane@example.com</a></td>"
            "</tr></table><script>alert(1)</script>")
    fields = parsers.label_values(parsers.html_to_text(html))
    assert fields["Full name"] == "Jane Doe" and fields["Email"] == "jane@example.com"


def test_normalise_maps_aliases_and_keeps_other_answers() -> None:
    lead = parsers.normalise(parsers.label_values(_META))
    assert lead.values["name"] == "Jane Doe"
    assert lead.values["email"] == "Jane.Doe@Example.com"
    assert lead.values["phone"] == "(202) 555-0123"
    assert lead.form == "Spring roof check-ups"
    assert lead.call_consent is True
    assert lead.answers == {"What service do you need?": "Flat roof repair"}


@pytest.mark.parametrize(("payload", "consent"), [
    ({"email": "a@b.com", "call_consent": "yes"}, True),
    ({"email": "a@b.com", "consent_to_call": "on"}, True),
    ({"email": "a@b.com", "call_consent": "no"}, False),
    ({"email": "a@b.com", "I agree to receive calls": "true"}, True),
    ({"email": "a@b.com", "phone": "2025550123"}, False),  # a phone number is not consent
])
def test_call_consent_answer(payload: dict[str, str], consent: bool) -> None:
    assert parsers.normalise(payload).call_consent is consent


def test_web_form_extras() -> None:
    lead = parsers.normalise({"first_name": "Jane", "last_name": "Doe", "email": "j@x.com",
                              "submission_id": "sub-9", "campaign": "Fall promo",
                              "form": "Roof quote", "timezone": "America/Chicago"})
    assert lead.values["name"] == "Jane Doe"
    assert lead.values["timezone"] == "America/Chicago"
    assert (lead.submission_id, lead.campaign, lead.form) == ("sub-9", "Fall promo", "Roof quote")


def test_gmail_forwarding_confirmation_code() -> None:
    subject = "(#123456789) Gmail Forwarding Confirmation - Receive Mail from rebecca@acme.com"
    assert parsers.gmail_confirmation("forwarding-noreply@google.com", subject) == "123456789"
    assert parsers.gmail_confirmation("someone@google.com", subject) is None
    assert parsers.gmail_confirmation("forwarding-noreply@google.com", "Hello") is None


@pytest.mark.parametrize(("e164", "zone"), [
    ("+12025550123", "America/New_York"),
    ("+14155550100", "America/Los_Angeles"),
    ("+18884810711", None),   # toll-free: no single zone
    ("+12085550100", None),   # Idaho spans two zones: don't guess
    ("+442079460958", None),  # outside the US market
    (None, None),
])
def test_timezone_only_when_unambiguous(e164: str | None, zone: str | None) -> None:
    assert parsers.infer_timezone(e164) == zone


def test_trusted_sender() -> None:
    trusted = ["facebookmail.com"]
    assert parsers.trusted_sender("notification@facebookmail.com", trusted)
    assert parsers.trusted_sender("x@mail.facebookmail.com", trusted)
    assert not parsers.trusted_sender("x@facebookmail.com.evil.io", trusted)
    assert not parsers.trusted_sender("x@notfacebookmail.com", trusted)


# -- A call booked on the form (roofengine, 2026-11-06) ---------------------------------------

_NOV20_330PM_NY = datetime(2026, 11, 20, 20, 30, tzinfo=UTC)


@pytest.mark.parametrize("fields", [
    {"Booking Date": "2026-11-20", "Booking Time": "15:30", "Time zone": "America/New_York"},
    {"Preferred date": "11/20/2026", "Preferred time": "3:30 PM", "Timezone": "America/New_York"},
    {"Call date": "November 20, 2026", "Call time": "3:30pm", "Time Zone": "America/New_York"},
    {"Booking": "Friday, November 20, 2026 at 3:30 PM (America/New_York)"},
    {"Scheduled call": "2026-11-20 15:30 America/New_York"},
])
def test_a_booked_call_is_read_from_the_form(fields: dict[str, str]) -> None:
    lead = parsers.normalise({"Name": "Jane Doe", "Email": "jane@example.com", **fields})
    assert lead.booking == (_NOV20_330PM_NY, "America/New_York")


@pytest.mark.parametrize("fields", [
    {"Booking Date": "2026-11-20", "Booking Time": "15:30"},     # no zone, none to fall back on
    {"Booking Date": "2026-11-20", "Time zone": "America/New_York"},  # no time
    {"Booking": "next Friday afternoon", "Time zone": "America/New_York"},  # can't be read
    {"Booking Date": "2026-11-20", "Booking Time": "15:30", "Time zone": "Mars/Base"},
    {"Booking": "Skipped"},
    {"Message": "Can we talk on 2026-11-20 at 15:30 America/New_York?"},  # not a booking field
])
def test_no_booking_unless_date_time_and_zone_are_all_clear(fields: dict[str, str]) -> None:
    assert parsers.normalise({"Email": "jane@example.com", **fields}).booking is None


def test_the_phones_zone_fills_in_a_booking_without_one() -> None:
    lead = parsers.normalise({"Booking Date": "2026-11-20", "Booking Time": "15:30"})
    assert parsers.booking_at(lead, default_zone="America/New_York") == (
        _NOV20_330PM_NY, "America/New_York")


_ROOF_ENGINE = """Name: Zachary Albers
Company: Zachary albers
Email: albers.zachari@gmail.com
Phone: (907) 606-0779
Service: Google Ads (PPC)
Message: Work
Call scheduled: 2026-11-03 at 09:58 (America/Anchorage)
Meet link: https://meet.google.com/jtm-rjfr-khr
"""


def test_the_roof_engine_inquiry_email() -> None:
    """The real paid@isngs.com e-mail from the roofengine form (2026-11-04)."""
    lead = parsers.normalise(parsers.label_values(_ROOF_ENGINE))
    assert lead.values == {"name": "Zachary Albers", "email": "albers.zachari@gmail.com",
                           "phone": "(907) 606-0779"}
    assert lead.booking == (datetime(2026, 11, 3, 18, 58, tzinfo=UTC), "America/Anchorage")
    assert lead.answers["Meet link"] == "https://meet.google.com/jtm-rjfr-khr"
    assert lead.call_consent is False  # no consent line yet: email only, no AI call
