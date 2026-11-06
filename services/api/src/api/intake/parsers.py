"""Lead intake parsing (A10) -- pure, no I/O.

Turns a lead notification e-mail or a form payload into the A5 row values
(name / email / phone / timezone), the person's other form answers, the call
consent answer and the source ids. Validation itself is ``agents.imports.check_row``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as dtime
from email import message_from_bytes, policy
from email.message import EmailMessage
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from bs4 import BeautifulSoup
from phonenumbers import timezone as phone_timezone

from api.agents.imports import _ALIASES, _YES

_LINE = re.compile(r"^\s*(?P<label>[^:\n]{1,120}?)\s*:\s*(?P<value>.*?)\s*$")
_EXTRA_ALIASES = {
    "mobile_number": "phone", "cell_phone": "phone", "phone_no": "phone", "work_phone": "phone",
    "email_address": "email",
}
_SUBMISSION = {"submission_id", "lead_id", "leadgen_id", "entry_id", "response_id"}
_FORM = {"form", "form_name"}
_CAMPAIGN = {"campaign", "campaign_name", "utm_campaign"}
_CONSENT_YES = {*_YES, "on", "agree", "agreed", "i agree", "checked", "accepted"}


@dataclass
class Normalised:
    values: dict[str, str] = field(default_factory=dict)   # name, email, phone, timezone
    answers: dict[str, str] = field(default_factory=dict)  # everything else they told us
    call_consent: bool = False
    submission_id: str | None = None
    form: str | None = None
    campaign: str | None = None
    # A call they booked on the form itself (the site makes its own Meet invite).
    booking_date: date | None = None
    booking_time: dtime | None = None
    booking_zone: str | None = None

    @property
    def booking(self) -> tuple[datetime, str] | None:
        return booking_at(self)


def booking_at(lead: Normalised, default_zone: str | None = None
               ) -> tuple[datetime, str] | None:
    """``(start in UTC, zone)`` of the call booked on the form -- only when the date, the
    time and a real IANA zone are all clear (the form's, else ``default_zone``)."""
    zone = lead.booking_zone or lead.values.get("timezone") or default_zone
    if lead.booking_date is None or lead.booking_time is None or not _real_zone(zone):
        return None
    assert zone is not None  # noqa: S101 -- _real_zone checked it
    local = datetime.combine(lead.booking_date, lead.booking_time, ZoneInfo(zone))
    return local.astimezone(UTC), zone


def _real_zone(zone: str | None) -> bool:
    if not zone or "/" not in zone:
        return False
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


_BOOKING_WORDS = ("booking", "booked", "meeting", "call", "appointment", "preferred",
                  "scheduled", "schedule", "requested")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%d %B %Y")
_TIME_FORMATS = ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p")
_WEEKDAY = re.compile(r"^(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s*", re.I)
_ZONE_IN_TEXT = re.compile(r"\(?\b([A-Za-z]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b\)?")
_TIME_IN_TEXT = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\b\.?|\b\d{1,2}:\d{2}\b", re.I)


def _parse(text: str, formats: tuple[str, ...]) -> datetime | None:
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007 -- naive part; zone added later
        except ValueError:
            continue
    return None


def _date(text: str) -> date | None:
    parsed = _parse(_WEEKDAY.sub("", text.strip()).strip(" ,"), _DATE_FORMATS)
    return parsed.date() if parsed else None


def _time(text: str) -> dtime | None:
    parsed = _parse(text.strip().upper().replace(".", ""), _TIME_FORMATS)
    return parsed.time() if parsed else None


def _read_booking(out: Normalised, fields: dict[str, str]) -> None:
    """Fields named like a booking: separate date / time fields, or one with all of it
    ("Friday, November 20, 2026 at 3:30 PM (America/New_York)")."""
    for label, raw in fields.items():
        key, value = _key(label), str(raw).strip()
        if "zone" in key or "consent" in key or not value:
            continue
        if key not in ("date", "time") and not any(w in key for w in _BOOKING_WORDS):
            continue
        if "date" in key:
            out.booking_date = out.booking_date or _date(value)
        elif "time" in key:
            out.booking_time = out.booking_time or _time(value)
        else:
            zone = _ZONE_IN_TEXT.search(value)
            rest = _ZONE_IN_TEXT.sub("", value) if zone else value
            clock = _TIME_IN_TEXT.search(rest)
            if clock is None:
                continue
            day = _date(re.sub(r"\s+at\s*$", "", rest[:clock.start()].strip(), flags=re.I))
            at = _time(clock.group(0))
            if day and at:
                out.booking_date, out.booking_time = day, at
                out.booking_zone = zone.group(1) if zone else out.booking_zone


def _key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return soup.get_text("\n")


def notification_text(raw: bytes) -> str:
    """A notification e-mail's text: the plain part, else the HTML flattened line by line."""
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)  # noqa: S101 -- policy.default builds EmailMessage
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return str(plain.get_content())
    html = msg.get_body(preferencelist=("html",))
    return html_to_text(str(html.get_content())) if html is not None else ""


def label_values(text: str) -> dict[str, str]:
    """``Label: value`` lines (or the value on the next line), first one wins."""
    lines = [line for line in (raw.strip() for raw in text.splitlines()) if line]
    found: dict[str, str] = {}
    i = 0
    while i < len(lines):
        match = _LINE.match(lines[i])
        if match is None or "http" in match["label"].lower():
            i += 1
            continue
        label, value = match["label"], match["value"]
        if not value and i + 1 < len(lines) and _LINE.match(lines[i + 1]) is None:
            value, i = lines[i + 1], i + 1
        if value and label not in found:
            found[label] = value
        i += 1
    return found


def _is_call_consent(key: str) -> bool:
    if key in ("call_consent", "consent_to_call"):
        return True
    return any(w in key for w in ("agree", "consent", "permission")) and (
        "call" in key or "phone" in key)


def normalise(fields: dict[str, str]) -> Normalised:
    out = Normalised()
    first = last = None
    for label, raw in fields.items():
        value = str(raw).strip()
        key = _key(label)
        canonical = _EXTRA_ALIASES.get(key) or _ALIASES.get(key)
        if _is_call_consent(key):
            out.call_consent = out.call_consent or value.lower() in _CONSENT_YES
        elif key in _SUBMISSION:
            out.submission_id = value or None
        elif key in _FORM:
            out.form = value or None
        elif key in _CAMPAIGN:
            out.campaign = value or None
        elif canonical == "first_name":
            first = value
        elif canonical == "last_name":
            last = value
        elif canonical in ("name", "email", "phone", "timezone"):
            out.values.setdefault(canonical, value)
        elif canonical is None and value:
            out.answers[label] = value[:500]
    if "name" not in out.values and (first or last):
        out.values["name"] = " ".join(p for p in (first, last) if p)
    _read_booking(out, fields)
    return out


def gmail_confirmation(from_addr: str, subject: str) -> str | None:
    """Gmail's forwarding-address confirmation code -- the admin must confirm it in Gmail."""
    if from_addr.strip().lower() != "forwarding-noreply@google.com":
        return None
    match = re.search(r"\(#(\d{5,12})\)", subject)
    return match.group(1) if match else None


def trusted_sender(from_addr: str, trusted: list[str]) -> bool:
    domain = from_addr.rsplit("@", 1)[-1].strip().lower().rstrip(">")
    return any(domain == t.lower() or domain.endswith("." + t.lower()) for t in trusted)


def infer_timezone(e164: str | None) -> str | None:
    """A US number's zone when its area code has exactly one; else ``None`` (A10 I4)."""
    if not e164:
        return None
    try:
        number = phonenumbers.parse(e164)
    except phonenumbers.NumberParseException:
        return None
    if phonenumbers.region_code_for_number(number) != "US":
        return None
    zones = phone_timezone.time_zones_for_number(number)
    return zones[0] if len(zones) == 1 and zones[0] != "Etc/Unknown" else None
