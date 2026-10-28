"""The call script and turn rules (A6 L4-L6) -- pure, no I/O, unit-testable.

``classify`` reads one caller utterance and returns what it is *before* any LLM
runs; ``CallState`` is the per-call memory persisted on ``call_sessions.session``
between Plivo turn webhooks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from email_validator import EmailNotValidError, validate_email

from api.voice_agent.agent import _GOODBYE_RE, _PERSON_RE

Kind = Literal["recording_objection", "opt_out", "wrong_number", "robot", "callback", "person",
               "goodbye", "not_interested", "book", "yes", "no", "other"]

_OPT_OUT = re.compile(
    r"\b(?:don'?t|do not|stop|never)\b.{0,20}\b(?:call|calling|contact|ring)\b"
    r"|\bremove (?:me|my number)\b|\btake me off\b|\bdo not call list\b|\bunsubscribe\b", re.I)
_WRONG = re.compile(r"\bwrong number\b|\bno one (?:here )?(?:by|named|called) that\b"
                    r"|\bthere'?s no \w+ here\b", re.I)
_RECORDING = re.compile(r"\b(?:don'?t|do not|stop|not|no)\b.{0,15}\brecord", re.I)
_ROBOT = re.compile(r"\b(?:are you|is this|am i (?:talking|speaking) (?:to|with))\b.{0,12}"
                    r"\b(?:robot|bot|machine|ai|a\.i\.|real person|human|recording|computer)\b",
                    re.I)
_CALLBACK = re.compile(
    r"\bcall (?:me )?(?:back|later|another time|tomorrow|next week)\b"
    r"|\b(?:not a good time|bad time|busy right now|i'?m busy|in a meeting|can'?t talk"
    r"|driving)\b", re.I)
_BOOK = re.compile(
    r"\b(?:book|schedule|set up|arrange|make)\b.{0,25}"
    r"\b(?:call|meeting|appointment|time|inspection|visit|consultation)\b"
    r"|\b(?:sounds good|i'?m interested|let'?s do it|sign me up)\b", re.I)
_NOT_INTERESTED = re.compile(
    r"\bnot interested\b|\bno,? thanks?\b|\bno thank you\b|\balready (?:have|found|hired|got)\b"
    r"|\bnot (?:looking|in the market)\b", re.I)
_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|correct|that works|perfect|ok(?:ay)?|please"
                  r"|book it|go ahead|sounds good)\b", re.I)
_NO = re.compile(r"^\s*(?:no|nope|nah|not really|neither)\b", re.I)


def classify(text: str) -> Kind:
    """What the caller said, by fixed rules, in priority order."""
    if _RECORDING.search(text):
        return "recording_objection"
    if _OPT_OUT.search(text):
        return "opt_out"
    if _WRONG.search(text):
        return "wrong_number"
    if _ROBOT.search(text):
        return "robot"
    if _CALLBACK.search(text):
        return "callback"
    if _PERSON_RE.search(text):
        return "person"
    if _GOODBYE_RE.search(text):
        return "goodbye"
    if _NOT_INTERESTED.search(text):
        return "not_interested"
    if _BOOK.search(text):
        return "book"
    # A bare yes/no only: "Sure, how much is a repair?" is a question, not a "yes".
    short = len(text.split()) <= 4 and "?" not in text
    if short and _YES.search(text):
        return "yes"
    if short and _NO.search(text):
        return "no"
    return "other"


# -- Lines (code-owned; the model never writes these) -------------------------------------


def disclosure(*, first_name: str | None, company: str, recording: bool) -> str:
    who = f" {first_name}" if first_name else ""
    line = f"Hi{who}, this is Rebecca, an AI assistant calling on behalf of {company}."
    return f"{line} This call is recorded for quality." if recording else line


def inbound_greeting(*, first_name: str | None, company: str, recording: bool) -> str:
    who = f" {first_name}" if first_name else ""
    line = f"Hi{who}, thanks for calling {company}. This is Rebecca, an AI assistant."
    if recording:
        line += " This call is recorded for quality."
    return f"{line} How can I help?"


def whisper(*, caller: str, asked: str | None) -> str:
    """What the team member hears before the caller is connected (A7 N6)."""
    about = f" They asked: {asked}" if asked else ""
    return f"Rebecca transferring a caller: {caller}.{about} Connecting you now."


PURPOSE = ("You recently got in touch with us, and I'm calling to answer any questions and help "
           "you book a time with our team if that's useful. Is now a good time?")
ROBOT_LINE = "Yes, I'm an AI assistant. I can answer questions or book a time with our team."
RECORDING_STOPPED = "No problem, I've stopped the recording."
OPT_OUT_LINE = "Understood. I've removed your number and we won't call again. Goodbye."
WRONG_NUMBER_LINE = "I'm sorry for the trouble. I'll remove this number. Goodbye."
CALLBACK_LINE = "No problem, I'll call back another time. Have a good day, goodbye."
GOODBYE_LINE = "Thanks for your time. Have a great day, goodbye."
NOT_INTERESTED_LINE = "Understood, thanks for letting me know. Have a great day, goodbye."
SILENCE_LINE = "Sorry, I didn't catch that. Are you still there?"
INBOUND_OFF_LINE = ("Thanks for calling. We can't take calls right now, please call back later "
                    "or visit our website. Goodbye.")
TRANSFER_MISSED_LINE = ("Sorry, our team couldn't pick up right now. Someone will call you back "
                        "soon. Goodbye.")
TEAM_LINE = ("Someone from our team will get back to you shortly. Thanks for your time, goodbye.")
TRANSFER_LINE = "Let me connect you with a member of our team. One moment, please."
MISS_LINE = "That's a good question for our team."
LINK_LINE = ("I'll have our team send you a link to pick a time that suits you. Thanks, goodbye.")
NO_SLOTS_LINE = ("I don't see an open time right now, so I'll have our team reach out to find one. "
                 "Thanks, goodbye.")
WRAP_UP_LINE = "I want to respect your time, so I'll have our team follow up. Thanks, goodbye."
ASK_WHICH = "Which works better for you?"
ANYTHING_ELSE = "Is there anything else I can help with?"
NOT_OPEN = "I'm sorry, we're not available at that time. "
DAY_FULL = "I'm sorry, we're not available that day. "
DAY_BOOKED = "I'm sorry, that day is already fully booked. "
OTHER_TIME_LINE = "No problem. What day and time would suit you better?"
EMAIL_ASK_LINE = ("What email address should I send the meeting link to? "
                  "Please say it slowly, like john dot smith at gmail dot com.")
EMAIL_RETRY_LINE = "Sorry, I didn't catch that. Could you say the email address again, slowly?"
SYNC_FAILED_LINE = ("I couldn't confirm that time in our calendar, so our team will reach out to "
                    "book it with you. Thanks, goodbye.")

_ZONE_NAMES = {"America/New_York": "Eastern", "America/Detroit": "Eastern",
               "America/Chicago": "Central", "America/Denver": "Mountain",
               "America/Phoenix": "Arizona", "America/Los_Angeles": "Pacific",
               "America/Anchorage": "Alaska", "Pacific/Honolulu": "Hawaii",
               "Asia/Kolkata": "India"}


def say_time(at: datetime, timezone: str) -> str:
    local = at.astimezone(ZoneInfo(timezone))
    minutes = "" if local.minute == 0 else f":{local:%M}"
    return (f"{local:%A}, {local:%B} {local.day} at {local.hour % 12 or 12}{minutes} "
            f"{'AM' if local.hour < 12 else 'PM'} {_ZONE_NAMES.get(timezone, timezone)} time")


def offer_line(slots: list[datetime], timezone: str) -> str:
    if len(slots) == 1:
        return f"I can do {say_time(slots[0], timezone)}. Does that work for you?"
    return (f"I have {say_time(slots[0], timezone)}, or {say_time(slots[1], timezone)}. "
            f"{ASK_WHICH}")


def taken_line(slots: list[datetime], timezone: str) -> str:
    """The time asked for already has a meeting: apologise, give the nearest, invite another."""
    times = " or ".join(say_time(s, timezone) for s in slots)
    which = "either of those" if len(slots) > 1 else "that"
    return (f"I'm sorry, we already have a meeting scheduled at that time. The closest I have "
            f"open is {times}. Would {which} work, or would you prefer another day and time?")


def confirm_line(slot: datetime, timezone: str) -> str:
    return f"Great. Just to confirm, {say_time(slot, timezone)}. Shall I book that?"


def ask_time_line(earliest: datetime, timezone: str) -> str:
    return (f"Sure. What day and time works best for you? The earliest I have is "
            f"{say_time(earliest, timezone)}.")


def email_check_line(email: str) -> str:
    return f"I'll send the meeting details to {say_email(email)}. Is that the right email?"


def email_heard_line(email: str) -> str:
    return f"I heard {say_email(email)}. Is that right?"


def booked_line(slot: datetime, timezone: str, *, emailed: bool = True,
                link: bool = False) -> str:
    when = f"You're booked for {say_time(slot, timezone)}."
    if not emailed:
        return f"{when} Our team will send you the meeting details. {ANYTHING_ELSE}"
    if link:
        return (f"{when} I'm sending the meeting link to your email now, and you'll get a "
                f"reminder before the call. {ANYTHING_ELSE}")
    return f"{when} You'll get a confirmation email and a reminder before the call. {ANYTHING_ELSE}"


_FIRST = re.compile(r"\b(?:first|1st|earlier|former|the one)\b", re.I)
_SECOND = re.compile(r"\b(?:second|2nd|later one|latter|other one|last one)\b", re.I)
_HOUR = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", re.I)


def choose_slot(text: str, slots: list[datetime], timezone: str) -> int | None:
    """Which offered slot the caller picked, or ``None`` if it's unclear."""
    if not slots:
        return None
    if len(slots) == 1 and _YES.search(text):
        return 0
    if _SECOND.search(text) and len(slots) > 1:
        return 1
    if _FIRST.search(text):
        return 0
    local = [s.astimezone(ZoneInfo(timezone)) for s in slots]
    days = [i for i, s in enumerate(local) if f"{s:%A}".lower() in text.lower()]
    if len(days) == 1:
        return days[0]
    for match in _HOUR.finditer(text):
        hour = int(match.group(1)) % 12
        suffix = (match.group(3) or "").lower()
        hits = [i for i, s in enumerate(local)
                if s.hour % 12 == hour and (not suffix or (s.hour >= 12) == suffix.startswith("p"))]
        if len(hits) == 1:
            return hits[0]
    return None


# -- What the caller asks for: a day/time, an email address ---------------------------------

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH = "|".join(_MONTHS)
_NUMBERS = {w: str(i) for i, w in enumerate(
    ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
     "twelve"), start=1)}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBERS) + r")\b")
_MONTH_DAY = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b")
_ORDINAL = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b")
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?|o'?clock)?")
_TIME_WORDS = {"at", "around", "about", "by", "morning", "afternoon", "evening", "night"}


def _on_or_after(today: date, year: int, month: int, day: int) -> date | None:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    return found if found >= today else None


def parse_when(text: str, today: date) -> tuple[date | None, tuple[int, int] | None]:
    """The day and (hour, minute) a caller asked for, each ``None`` when not said.
    Hours 1-7 without am/pm mean the afternoon (nobody books a 3 AM call)."""
    t = _NUMBER_WORD.sub(lambda m: _NUMBERS[m.group(1)], text.lower())
    day: date | None = None
    if (m := _MONTH_DAY.search(t)) or (m := _DAY_MONTH.search(t)):
        month_name, number = ((m.group(1), m.group(2)) if m.re is _MONTH_DAY
                              else (m.group(2), m.group(1)))
        month = _MONTHS.index(month_name) + 1
        day = (_on_or_after(today, today.year, month, int(number))
               or _on_or_after(today, today.year + 1, month, int(number)))
        t = t[:m.start()] + " " + t[m.end():]
    elif m := _ORDINAL.search(t):
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        day = (_on_or_after(today, today.year, today.month, int(m.group(1)))
               or _on_or_after(today, nxt.year, nxt.month, int(m.group(1))))
        t = t[:m.start()] + " " + t[m.end():]
    elif "day after tomorrow" in t:
        day = today + timedelta(days=2)
    elif "tomorrow" in t:
        day = today + timedelta(days=1)
    elif re.search(r"\btoday\b|\btonight\b", t):
        day = today
    else:
        for i, name in enumerate(_WEEKDAYS):
            if re.search(rf"\b{name}\b", t):
                day = today + timedelta(days=(i - today.weekday()) % 7)
                break

    at: tuple[int, int] | None = None
    for c in _CLOCK.finditer(t):
        before = t[:c.start()].split()
        if not (c.group(2) or c.group(3) or (before and before[-1] in _TIME_WORDS)):
            continue  # a bare number ("the first one") isn't a time
        hour, minute, suffix = int(c.group(1)), int(c.group(2) or 0), (c.group(3) or "")[:1]
        if hour > 23 or minute > 59:
            continue
        if suffix == "p" and hour < 12:
            hour += 12
        elif suffix == "a" and hour == 12:
            hour = 0
        elif suffix not in ("a", "p") and hour < 12:
            later = re.search(r"\b(?:afternoon|evening|night)\b", t)
            if later or (1 <= hour <= 7 and "morning" not in t):
                hour += 12
        at = (hour, minute)
        break
    if at is None and re.search(r"\bnoon\b", t):
        at = (12, 0)
    return day, at


_EMAIL_PREFIX = re.compile(
    r"^(?:(?:yes|yeah|sure|ok(?:ay)?|no|nope|actually)[,.]?\s+)?"
    r"(?:it'?s|it is|that'?s|my email(?: address)? is"
    r"|the email(?: address)? is|email is)\s+")


def parse_spoken_email(text: str) -> str | None:
    """An email address from what the caller said ("john dot smith at gmail dot com"),
    or ``None`` when it isn't one. Indian English "at the rate" is understood too."""
    t = _EMAIL_PREFIX.sub("", text.lower().strip())
    t = re.sub(r"\bat the rate(?: of)?\b|\bat\b", "@", t)
    t = re.sub(r"\b(?:dot|period|point)\b", ".", t)
    t = re.sub(r"\bunderscore\b", "_", t)
    t = re.sub(r"\b(?:dash|hyphen)\b", "-", t)
    t = re.sub(r"\s+", "", t).strip(".,")
    try:
        return validate_email(t, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def say_email(email: str) -> str:
    for char, word in ((".", " dot "), ("@", " at "), ("_", " underscore "), ("-", " dash ")):
        email = email.replace(char, word)
    return " ".join(email.split())


# -- Per-call memory ------------------------------------------------------------------------

Stage = Literal["talking", "asking_time", "offered", "confirming", "email_check", "email_ask"]


@dataclass
class CallState:
    stage: Stage = "talking"
    offered: list[str] = field(default_factory=list)  # ISO datetimes
    chosen: str | None = None
    email: str | None = None  # where the meeting details go
    email_tries: int = 0
    silences: int = 0
    misses: int = 0
    recording_started: bool = False
    history: list[list[str]] = field(default_factory=list)  # [role, text]

    def to_json(self) -> dict[str, Any]:
        return {"stage": self.stage, "offered": self.offered, "chosen": self.chosen,
                "email": self.email, "email_tries": self.email_tries,
                "silences": self.silences, "misses": self.misses,
                "recording_started": self.recording_started, "history": self.history[-12:]}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> CallState:
        data = data or {}
        return cls(stage=data.get("stage", "talking"), offered=list(data.get("offered", [])),
                   chosen=data.get("chosen"), email=data.get("email"),
                   email_tries=int(data.get("email_tries", 0)),
                   silences=int(data.get("silences", 0)),
                   misses=int(data.get("misses", 0)),
                   recording_started=bool(data.get("recording_started", False)),
                   history=[list(h) for h in data.get("history", [])])
