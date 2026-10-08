"""AI voice confirmation call -- pure helpers + the booking-time entry point.

Flow: a booking with a phone number (scheduling route) -> ``schedule_booking_call``
creates a ``voice_calls`` row -> Celery ``calls.place_voice_call`` asks Twilio
to dial (``voice_tasks``) -> Twilio fetches TwiML from ``voice_webhook``: each
yes/no question is spoken with ``<Say>`` inside a ``<Gather>`` (speech or
keypad 1/2) whose answer posts back, is stored, and gets the next question.
An unclear/missing answer is recorded as such and the call moves on -- the
visitor is never asked the same question twice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

from common.auth import AuthClaims
from common.db import Database

from api.calls.voice_repository import (
    DEFAULT_QUESTIONS,
    create_voice_call,
    get_voice_call_config,
)
from api.config import get_api_settings
from api.notifications.config_repository import get_notification_config_by_tenant_id

INTRO = "Hi, this is an automated call to confirm the call you just booked with us."
ANSWER_HINT = "Please say yes or no, or press 1 for yes and 2 for no."
GOODBYE = "Thank you, your answers have been saved. Goodbye."

_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "correct", "absolutely", "definitely", "ok", "okay",
}
_NO_WORDS = {"no", "nope", "nah", "not", "never", "don't", "dont", "negative"}


def to_e164(phone: str) -> str | None:
    """Normalise a typed phone number for Twilio. Ten digits are read as a US/
    Canada number (the widget's phone field is US-formatted); anything else
    must already carry its +country code. ``None`` when it can't be dialled."""
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def render_questions(questions: list[str], starts_at: datetime, timezone: str) -> list[str]:
    """Fill ``{date}``/``{time}`` with the booked slot, in the booking's timezone."""
    local = starts_at.astimezone(ZoneInfo(timezone))
    hour = local.hour % 12 or 12
    date_text = f"{local:%A, %B} {local.day}"
    time_text = f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return [q.replace("{date}", date_text).replace("{time}", time_text) for q in questions]


def parse_yes_no(speech: str | None, digits: str | None) -> str:
    """'yes' / 'no' / 'unclear' / 'no_response' from Twilio's Gather result."""
    if digits:
        return {"1": "yes", "2": "no"}.get(digits.strip(), "unclear")
    if not speech or not speech.strip():
        return "no_response"
    words = set(re.findall(r"[a-z']+", speech.lower()))
    said_yes, said_no = bool(words & _YES_WORDS), bool(words & _NO_WORDS)
    if said_yes == said_no:
        return "unclear"
    return "yes" if said_yes else "no"


def question_twiml(*, question: str, action_url: str, voice: str, intro: bool) -> str:
    text = f"{INTRO} {question} {ANSWER_HINT}" if intro else f"{question} {ANSWER_HINT}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="speech dtmf" numDigits="1" timeout="6" speechTimeout="auto" '
        f'language="en-US" hints="yes, no" actionOnEmptyResult="true" method="POST" '
        f"action={quoteattr(action_url)}>"
        f"<Say voice={quoteattr(voice)}>{escape(text)}</Say>"
        "</Gather></Response>"
    )


def goodbye_twiml(voice: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Say voice={quoteattr(voice)}>{escape(GOODBYE)}</Say><Hangup/></Response>"
    )


@dataclass(frozen=True)
class TwilioCredentials:
    account_sid: str
    auth_token: str
    from_number: str


async def twilio_credentials_for(db: Database, tenant_id: str) -> TwilioCredentials | None:
    """The chatbot's own Twilio (its SMS config) if set, else the platform's."""
    sms = await get_notification_config_by_tenant_id(db, tenant_id, channel="sms")
    if (
        sms is not None
        and sms.provider == "twilio"
        and sms.twilio_account_sid
        and sms.credentials
        and sms.twilio_from
    ):
        return TwilioCredentials(sms.twilio_account_sid, sms.credentials, sms.twilio_from)
    s = get_api_settings()
    sid, token, number = (
        s.platform_twilio_account_sid, s.platform_twilio_auth_token, s.platform_twilio_from_number,
    )
    if sid and token and number:
        return TwilioCredentials(sid, token, number)
    return None


async def schedule_booking_call(
    db: Database,
    claims: AuthClaims,
    *,
    event_id: str,
    lead_id: str | None,
    phone: str | None,
    starts_at: datetime,
    timezone: str,
) -> str | None:
    """Create the confirmation-call row for a new booking; returns its ``call_id``
    (the caller enqueues the Celery task) or ``None`` when no call applies:
    feature off for this chatbot, no/undiallable phone, or already created."""
    config = await get_voice_call_config(db, claims)
    if config is None or not config.enabled or not phone:
        return None
    to_number = to_e164(phone)
    if to_number is None:
        return None
    questions = render_questions(config.questions or DEFAULT_QUESTIONS, starts_at, timezone)
    return await create_voice_call(
        db, claims, event_id=event_id, lead_id=lead_id, to_number=to_number, questions=questions,
    )
