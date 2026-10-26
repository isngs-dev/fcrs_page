"""Lead intake parsing (A10) -- pure, no I/O.

Turns a lead notification e-mail or a form payload into the A5 row values
(name / email / phone / timezone), the person's other form answers, the call
consent answer and the source ids. Validation itself is ``agents.imports.check_row``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage

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
