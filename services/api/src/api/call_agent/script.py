"""The call script and turn rules (A6 L4-L6) -- pure, no I/O, unit-testable.

``classify`` reads one caller utterance and returns what it is *before* any LLM
runs; ``CallState`` is the per-call memory persisted on ``call_sessions.session``
between Plivo turn webhooks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

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

_ZONE_NAMES = {"America/New_York": "Eastern", "America/Detroit": "Eastern",
               "America/Chicago": "Central", "America/Denver": "Mountain",
               "America/Phoenix": "Arizona", "America/Los_Angeles": "Pacific",
               "America/Anchorage": "Alaska", "Pacific/Honolulu": "Hawaii"}


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


def confirm_line(slot: datetime, timezone: str) -> str:
    return f"Great. Just to confirm, {say_time(slot, timezone)}. Shall I book that?"


def booked_line(slot: datetime, timezone: str) -> str:
    return (f"You're booked for {say_time(slot, timezone)}. You'll get a confirmation email and "
            f"a reminder before the call. {ANYTHING_ELSE}")


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


# -- Per-call memory ------------------------------------------------------------------------

Stage = Literal["talking", "offered", "confirming"]


@dataclass
class CallState:
    stage: Stage = "talking"
    offered: list[str] = field(default_factory=list)  # ISO datetimes
    chosen: str | None = None
    silences: int = 0
    misses: int = 0
    recording_started: bool = False
    history: list[list[str]] = field(default_factory=list)  # [role, text]

    def to_json(self) -> dict[str, Any]:
        return {"stage": self.stage, "offered": self.offered, "chosen": self.chosen,
                "silences": self.silences, "misses": self.misses,
                "recording_started": self.recording_started, "history": self.history[-12:]}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> CallState:
        data = data or {}
        return cls(stage=data.get("stage", "talking"), offered=list(data.get("offered", [])),
                   chosen=data.get("chosen"), silences=int(data.get("silences", 0)),
                   misses=int(data.get("misses", 0)),
                   recording_started=bool(data.get("recording_started", False)),
                   history=[list(h) for h in data.get("history", [])])
