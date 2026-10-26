"""A10 intake parsing -- pure functions: notification e-mail fields, form payload
normalisation, consent answers, Gmail forwarding confirmation, timezone inference."""
from __future__ import annotations

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
