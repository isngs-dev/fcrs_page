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

Kind = Literal["recording_objection", "opt_out", "wrong_number", "robot", "who", "why", "hold",
               "callback",
               "person",
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
# "Who is this calling?" / "What was your name again?" -- answered by the code with her name
# and the business (live 2026-11-16: the model said "I don't have a personal name").
_WHO = re.compile(r"\bwho(?:'s| is| am i| are you)\b.{0,20}\b(?:this|calling|speaking|talking)\b"
                  r"|\byour name\b", re.I)
# "Can you hold for a 2nd?" / "hang on" / "one sec" -- they step away for a moment
# (live 2026-11-16: two 8-second silences would have hung up on a longer hold).
_HOLD = re.compile(r"\b(?:hold|hang) on\b|\bhold for\b"
                   r"|\b(?:one|1|just a|wait a|give me a) (?:sec|second|2nd|minute|moment)\b"
                   r"|^\W*(?:one|1) (?:sec|second|2nd)\b", re.I)
_ASKS_SOMETHING = re.compile(r"\b(?:what|how|why|when|where|which|much|price|cost|need)\b", re.I)
# "You called me." / "Why are you calling?" / "What's this about?" -- answered by the code
# with the real reason (live 2026-11-16: the model talked about "that earlier call").
_WHY = re.compile(r"\byou (?:had |have |just )?called me\b"
                  r"|\bwhy (?:are|were|did) you (?:calling|call|ring)\b(?! it)"
                  r"|\bwhat(?:'s| is) (?:this|the) call (?:about|regarding|for)\b"
                  r"|\bwhat(?:'s| is) this (?:about|regarding)\b", re.I)
_CALLBACK = re.compile(
    r"\bcall (?:me )?(?:back|later|another time|tomorrow|next week)\b"
    r"|\b(?:not a good time|bad time|busy right now|i'?m busy|in a meeting|can'?t talk"
    r"|driving)\b", re.I)
_BOOK = re.compile(
    r"\b(?:book|schedule|set up|arrange|make)\b.{0,25}"
    r"\b(?:call|meeting|appointment|time|inspection|visit|consultation|demo|sales|team|book)\b"
    r"|\b(?:sounds good|i'?m interested|let'?s do it|sign me up)\b", re.I)
_NOT_INTERESTED = re.compile(
    r"\bnot interested\b|\bno,? thanks?\b|\bno thank you\b|\balready (?:have|found|hired|got)\b"
    r"|\bnot (?:looking|in the market)\b", re.I)
_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|correct|that works|perfect|ok(?:ay)?|please"
                  r"|book it|go ahead|go with|do it|right|that'?s (?:right|correct|fine|good)"
                  r"|sounds good)\b", re.I)
_NO = re.compile(r"^\s*(?:no|nope|nah|not really|neither)\b", re.I)
_ASKS = re.compile(r"\?|^\s*(?:how|what|when|where|why|who|which|do|does|did|can|could|is|are"
                   r"|will|would|should)\b", re.I)


def is_question(text: str) -> bool:
    """Phone speech rarely carries a "?" -- a leading question word counts too."""
    return bool(_ASKS.search(text))


_WANTS_INFO = re.compile(
    r"\b(?:want|wanna|like|need|love|have) to (?:know|learn|understand|hear)\b"
    r"|\btell me\b|\bexplain\b|\bwalk me through\b|\bmore (?:about|info|information|details)\b"
    r"|\binfo(?:rmation)? (?:about|on)\b|\bcurious (?:about|how|what)\b", re.I)


_SEE_YOU_THEN = re.compile(
    r"\b(?:i'?ll|i will|we'?ll|we will)\b.{0,30}\b(?:talk|speak|see|meet|chat)\b"
    r"|\bsee you\b|\btalk (?:to you |with you )?(?:then|soon|later)\b"
    r"|\bon (?:the|our|my|that) (?:call|meeting)\b|\bno,? thank(?:s| you)\b", re.I)


def closes_with_booking(text: str) -> bool:
    """With a call booked: "I will talk with your team on Monday", "see you then", "no, thank
    you" -- a goodbye, not a transfer request or a "not interested" (live 2026-11-12)."""
    return bool(_SEE_YOU_THEN.search(text)) and "?" not in text


_DECLINES_BOOKING = re.compile(
    r"\balready (?:booked|have|got|scheduled|set|moved|made)\b"
    r"|\bdon'?t (?:want|need) (?:to|a|another)\b|\bdon'?t (?:book|schedule|set up|arrange)\b"
    r"|\bnot now\b|\bno need\b|\bnever ?mind\b", re.I)
_ABOUT_RESULTS = re.compile(r"\bguarantee|\bpromise|\bhow many\b|\bhow much\b|\bresults?\b",
                            re.I)


def declines_booking(text: str) -> bool:
    """Mid-booking: "I have already booked, I don't want to repeat it" -- stop asking for a
    time (live 2026-11-13: asked three times, then handed off)."""
    return bool(_DECLINES_BOOKING.search(text))


def asks_about_results(text: str) -> bool:
    """ "Do you guarantee booked inspections?" mentions booking but asks about results: answer
    it, don't start a booking (live 2026-11-13)."""
    return is_question(text) and bool(_ABOUT_RESULTS.search(text))


_DONE = re.compile(
    r"^\W*nothing\b|\b(?:got|have|had) (?:all )?(?:the |my )?answers?\b"
    r"|\bno (?:more|other|further) questions?\b|\bthat'?s everything\b|\ball set\b", re.I)
_FAREWELL = re.compile(r"\bthanks for your time\b|\btake care\b|\bgoodbye\b"
                       r"|\bhave a (?:good|great|nice|lovely) (?:day|one|evening)\b", re.I)


# Her questions a bare "yes" can't answer by itself: "anything else?", a booking offer, the
# fixed checks. Any other question of hers was written for this conversation.
_OPEN_ENDED = re.compile(
    r"\banything (?:else|more)\b|\bwhat (?:else|questions)\b|\bclear up\b|\bfind a time\b"
    r"|\bbook a time\b|\bkeep that time\b|\bgood time\b|\bshall i book\b|\bstill there\b"
    r"|\banything you'?d like to know\b|\bday and time\b|\bwhat (?:day|time)\b", re.I)


def her_question(line: str) -> str | None:
    """Her last line ended on a question of her own ("Would you like to know what it can
    handle?"): a "yes" answers *that* -- it isn't "what questions can I answer?" again
    (live 2026-11-14). ``None`` for the open-ended and code-owned questions."""
    last = re.split(r"(?<=[.!?])\s+", line.strip())[-1]
    return last if last.endswith("?") and not _OPEN_ENDED.search(last) else None


_REPEAT = re.compile(
    r"\bsay that again\b|\bnot sure what you mean\b|\bdidn'?t (?:quite )?(?:catch|get|understand)\b"
    r"|\b(?:could|can|would) you (?:please )?(?:repeat|rephrase|clarify)\b|\bwhat do you mean\b"
    r"|\bdid you mean\b|\bare you asking\b", re.I)


def asked_to_repeat(line: str) -> bool:
    """She didn't understand what was just said ("Could you say that again?")."""
    return bool(_REPEAT.search(line))


def understood_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The transcript without what she couldn't make sense of: a caller turn she answered with
    a request to repeat, and that request. A mishearing ("Meta ads" -> "my dad") must not
    become a fact in the notes, the summary or a later e-mail (live 2026-11-14)."""
    kept: list[dict[str, Any]] = []
    for turn in turns:
        if (turn["speaker"] != "caller" and asked_to_repeat(turn["text"])
                and kept and kept[-1]["speaker"] == "caller"):
            kept.pop()
            continue
        kept.append(turn)
    return kept


def is_done(text: str) -> bool:
    """ "Nothing. I've got the answers" -- they're finished: a goodbye (live 2026-11-14)."""
    return "?" not in text and bool(_DONE.search(text))


def said_farewell(line: str) -> bool:
    """Her last line closed the call ("Have a good day."): a "mhmm" back ends it."""
    return bool(_FAREWELL.search(line))


_VOICEMAIL = re.compile(
    r"\bleave (?:a|your) (?:message|name)\b|\bafter the (?:tone|beep)\b|\bvoice ?mail\b"
    r"|\bmailbox\b|\brecord your message\b", re.I)


def is_voicemail_greeting(text: str) -> bool:
    """ "We are not available, please leave a message" -- a machine, whatever Plivo's detector
    said (live 2026-11-14: CallHippo's greeting was answered like a person)."""
    return bool(_VOICEMAIL.search(text))


def wants_info(text: str) -> bool:
    """ "I want to know about ...", "tell me about ..." -- a question without a question word,
    to be answered, not thanked for (live 2026-11-12)."""
    return bool(_WANTS_INFO.search(text))


_TIME_OK = re.compile(
    r"\b(?:it'?s|it is|this is|now is|now'?s|that'?s)\s+(?:a\s+)?"
    r"(?:good|fine|great|ok(?:ay)?|perfect)\b"
    r"|\bi(?:'?ve| have| got)\s+(?:a\s+)?(?:few\s+)?(?:minutes?|time|a minute)\b"
    r"|\bgo ahead\b", re.I)


def time_is_ok(text: str) -> bool:
    """ "Yes, this is a good time", "now is fine", "I have a few minutes": a yes to "Is now a
    good time?" however long, and never their need (live 2026-11-19: Flux split "Yes, this is
    a good time" in two, and the tail became the booking email's agenda)."""
    return (bool(_TIME_OK.search(text)) and len(text.split()) <= 8 and not is_question(text)
            and not _CALLBACK.search(text))


_CHANGE_MIND = re.compile(r"\b(?:but|change|instead|actually|wait|not sure)\b", re.I)


def agrees(text: str) -> bool:
    """A yes to the final check, however it's put: "yes that's it" isn't a goodbye there."""
    return (bool(_YES.search(text)) and len(text.split()) <= 7 and "?" not in text
            and not _CHANGE_MIND.search(text))


# "I don't want to book a call" declines a booking; it isn't "don't call me" (live 2026-11-14).
_BOOKING_A_CALL = re.compile(
    r"\b(?:book|schedule|set up|arrange)\s+(?:a|the|another|that|this)?\s*(?:call|meeting)\b",
    re.I)


def classify(text: str) -> Kind:
    """What the caller said, by fixed rules, in priority order."""
    if _RECORDING.search(text):
        return "recording_objection"
    if _OPT_OUT.search(_BOOKING_A_CALL.sub(" ", text)):
        return "opt_out"
    if _WRONG.search(text):
        return "wrong_number"
    if _ROBOT.search(text):
        return "robot"
    if _WHO.search(text):
        return "who"
    if _WHY.search(text):
        return "why"
    # "Hold on, how much is a repair?" asks something: that's a question, not a hold.
    if _HOLD.search(text) and not _ASKS_SOMETHING.search(text) and len(text.split()) <= 9:
        return "hold"
    if _CALLBACK.search(text):
        return "callback"
    if _PERSON_RE.search(text):
        return "person"
    if _GOODBYE_RE.search(text) or is_done(text):
        return "goodbye"
    if _NOT_INTERESTED.search(text):
        return "not_interested"
    if _BOOK.search(text) and not declines_booking(text):
        return "book"
    # A bare yes/no only: "Sure, how much is a repair?" is a question, not a "yes".
    short = len(text.split()) <= 4 and "?" not in text
    if short and _YES.search(text):
        return "yes"
    if short and _NO.search(text):
        return "no"
    return "other"


# -- Lines (code-owned; the model never writes these) -------------------------------------


# Short on purpose: a ~48-word opening ran ~25 s and the person's "yes" was lost (2026-10-29).
RECORDED_LINE = "This call's recorded."
GENERIC_HOOK = "Thanks for reaching out to us recently."


def opening(*, first_name: str | None, company: str, recording: bool, hook: str | None) -> str:
    """The first thing said on an outbound call: who, the AI disclosure, why we're calling
    (``hook``: what they reached out about + what we do for businesses like theirs, written
    from their chat/form + this business's knowledge base), the recording notice, then a
    question that hands them the floor. 2026-11-07: shorter, value-first wording."""
    who = f" {first_name}" if first_name else ""
    parts = [f"Hi{who}, Rebecca here, the AI assistant at {company}.", hook or GENERIC_HOOK]
    if recording:
        parts.append(RECORDED_LINE)
    return " ".join([*parts, "Is now a good time?"])


def inbound_greeting(*, first_name: str | None, company: str, recording: bool) -> str:
    who = f" {first_name}" if first_name else ""
    parts = [f"Hi{who}, thanks for calling {company}. It's Rebecca, the AI assistant here."]
    if recording:
        parts.append(RECORDED_LINE)
    return " ".join([*parts, "What can I do for you today?"])


def whisper(*, caller: str, asked: str | None) -> str:
    """What the team member hears before the caller is connected (A7 N6)."""
    about = f" They asked: {asked}" if asked else ""
    return f"Rebecca transferring a caller: {caller}.{about} Connecting you now."


ROBOT_LINE = "Yes, I'm an AI assistant. I can answer questions or book a time with our team."


def why_line(company: str, booked_at: datetime | None, timezone: str | None) -> str:
    """Why she is calling, from the facts: their booked call, else their enquiry."""
    if booked_at is not None and timezone:
        return (f"Yes, I'm calling about your call with our team on "
                f"{say_time(booked_at, timezone)}, to make sure that time still works for you and "
                "to answer any questions before then.")
    return (f"Yes, I'm calling from {company} to follow up on your recent enquiry and to answer "
            "any questions you have.")


def who_line(company: str) -> str:
    return f"I'm Rebecca, the AI assistant at {company}. How can I help you today?"


RECORDING_STOPPED = "No problem, I've stopped the recording."
OPT_OUT_LINE = "Understood. I've removed your number and we won't call again. Goodbye."
WRONG_NUMBER_LINE = "I'm sorry for the trouble. I'll remove this number. Goodbye."
CALLBACK_LINE = "No problem, I'll call back another time. Have a good day, goodbye."
GOODBYE_LINE = "Thanks for your time. Have a great day, goodbye."
NOT_INTERESTED_LINE = "Understood, thanks for letting me know. Have a great day, goodbye."
SILENCE_LINE = "Sorry, I didn't catch that. Are you still there?"
HOLD_LINE = "Sure, take your time. I'll be right here."
HOLD_CHECK_LINE = "I'm still here whenever you're ready. Take your time."
HOLD_QUIET_TURNS = 6  # silent turns (about 8 s each) she waits quietly before checking in
INBOUND_OFF_LINE = ("Thanks for calling. We can't take calls right now, please call back later "
                    "or visit our website. Goodbye.")
TRANSFER_MISSED_LINE = ("Sorry, our team couldn't pick up right now. Someone will call you back "
                        "soon. Goodbye.")
TEAM_LINE = ("Someone from our team will get back to you shortly. Thanks for your time, goodbye.")
TRANSFER_LINE = "Let me connect you with a member of our team. One moment, please."
# When the model's own hand-off for the question isn't usable: the same idea, never the same
# words twice in a row (owner, 2026-11-16).
MISS_LINES = (
    "That's a great question. I believe our marketing expert will be more precise in answering "
    "that.",
    "Good question. Our marketing expert will be able to give you a more precise answer on that.",
    "That's a really good question, and our marketing expert can answer it more precisely than "
    "I can.",
)
MISS_LINE = MISS_LINES[0]


def miss_line(misses: int) -> str:
    """The fallback hand-off for the ``misses``-th question she couldn't answer (1-based)."""
    return MISS_LINES[(max(misses, 1) - 1) % len(MISS_LINES)]
LINK_LINE = ("I'll have our team send you a link to pick a time that suits you. Thanks, goodbye.")
NO_SLOTS_LINE = ("I don't see an open time right now, so I'll have our team reach out to find one. "
                 "Thanks, goodbye.")
WRAP_UP_LINE = "I want to respect your time, so I'll have our team follow up. Thanks, goodbye."
ANYTHING_ELSE = "Is there anything else I can help with?"
CLOSING = "Thanks for your time, and have a great day. Goodbye."
# After the meeting is settled she opens the floor instead of hanging up (owner, 2026-11-14).
INVITE = ("In the meantime, is there anything you'd like to know about our services, or "
          "anything I can clear up for you?")
# Asked once, only after a pause that follows an answer -- never tacked onto every answer
# (owner, 2026-11-16: the caller had no room to talk).
ANYTHING_ELSE_AFTER_PAUSE = "Is there anything else you'd like to know?"
CLOSE_OFFER_LINE = ("Before you go, our team can walk you through this in more detail on a "
                    "short call. Would you like me to find a time with them?")
# Owner, 2026-11-20: their questions first -- the call with the team is offered once they're
# done (CLOSE_OFFER_LINE), or when they ask for it; never right after their need or a miss.
DISCOVERY_THANKS_LINE = ("Thanks, that's really helpful. Is there anything you'd like to know "
                         "about how we can help with that?")
WHAT_QUESTIONS_LINE = "Great! What questions can I answer?"


def discovery_thanks_line(first_name: str | None) -> str:
    """Their name now and then, in her own lines too (owner, 2026-11-17)."""
    if not first_name:
        return DISCOVERY_THANKS_LINE
    return DISCOVERY_THANKS_LINE.replace("Thanks,", f"Thanks, {first_name},", 1)


# "Other" on the form and nothing in the message: ask, never guess (owner, 2026-11-17).
OTHER_SERVICE_QUESTION = "I saw you picked Other on our form. What can we help you with?"
NOTED_LINE = "Thanks, I've noted that."
def qualify_line(first_name: str | None, ask: str) -> str:
    """After they've said what they need: thanks, then one question she still needs (plan 1.2)."""
    return f"Thanks{f', {first_name}' if first_name else ''}, that's really helpful. {ask}"


def pause_question(ask: str) -> str:
    """The one follow-up after a pause (R33), spent on a question she still needs (plan 1.2)."""
    return f"If you don't mind me asking, {ask[:1].lower()}{ask[1:]}"


QUALIFY_DONE_LINE = f"Got it, thanks. {ANYTHING_ELSE_AFTER_PAUSE}"
QUALIFY_PER_CALL = 2  # at most this many qualification questions in one call

DISCOVERY_NO_LINE = "No problem. Is there anything I can answer for you about our services?"
WHICH_TIME = "What day and time works best for you?"
ASK_TIME_LINE = f"Sure. {WHICH_TIME}"
ASK_DAY_LINE = "And which day would you like?"
TIME_AGAIN = "Sorry, I didn't catch the time."
# Live 2026-11-20: the same "What day and time works best for you?" twice, then a hand-off.
TIME_EXAMPLE_LINE = ("Sorry, I didn't catch that. Could you tell me a day and a time, for "
                     "example, Monday at 2 PM?")
TIME_LINK_LINE = ("I'm having trouble catching the time over the phone, so I'll have our team "
                  "email you a link to pick one that suits you. Is there anything else I can "
                  "help with?")
TIME_TRIES = 3  # times she didn't catch a day or time before the link is offered
TAKEN_LINE = ("I'm sorry, we already have a meeting scheduled at that time. "
              "What other time would suit you?")
DAY_FULL = "I'm sorry, we're not available that day. What other day would suit you?"
DAY_BOOKED = "I'm sorry, that day is already fully booked. What other day would suit you?"
OTHER_TIME_LINE = "No problem. What day and time would suit you better?"
CHANGE_LINE = "No problem. What should I change, the email, or the day and time?"
EMAIL_ASK_LINE = ("What email should I send the booking details to? "
                  "Please say it slowly, or spell it out letter by letter.")
EMAIL_RETRY_LINE = ("Sorry, I want to get it exactly right. Could you spell the email for me, "
                    "letter by letter?")
EMAIL_TRIES = 3  # then it is booked anyway and the team sends the details
SYNC_FAILED_LINE = ("I couldn't confirm that time in our calendar, so our team will reach out to "
                    "book it with you. Thanks, goodbye.")

_ZONE_NAMES = {"America/New_York": "Eastern", "America/Detroit": "Eastern",
               "America/Chicago": "Central", "America/Denver": "Mountain",
               "America/Phoenix": "Arizona", "America/Los_Angeles": "Pacific",
               "America/Anchorage": "Alaska", "Pacific/Honolulu": "Hawaii",
               "Asia/Kolkata": "India"}


def zone_label(timezone: str) -> str | None:
    """ "Eastern" for America/New_York: how the zone is said and written; ``None`` if unlisted."""
    return _ZONE_NAMES.get(timezone)


def say_time(at: datetime, timezone: str) -> str:
    local = at.astimezone(ZoneInfo(timezone))
    minutes = "" if local.minute == 0 else f":{local:%M}"
    return (f"{local:%A}, {local:%B} {local.day} at {local.hour % 12 or 12}{minutes} "
            f"{'AM' if local.hour < 12 else 'PM'} {_ZONE_NAMES.get(timezone, timezone)} time")


def just_booked_line(at: datetime, timezone: str) -> str:
    """Option A (owner, 2026-11-20): they booked in the chat a moment ago and ticked the call."""
    return f"Thanks for booking a call with our team for {say_time(at, timezone)}."


def booked_question(at: datetime, timezone: str) -> str:
    """They already booked (e.g. on the website form): keep it or move it, never book again."""
    return (f"I see you've booked a call with our team for {say_time(at, timezone)}. "
            "Would you like to keep that time, or reschedule?")


def kept_line(at: datetime, timezone: str) -> str:
    return f"Great, you're all set for {say_time(at, timezone)}. {INVITE}"


def booked_goodbye_line(at: datetime, timezone: str) -> str:
    """The goodbye when a call with the team stands: their questions go there in detail."""
    return (f"Our team will go over everything in more detail on your call on "
            f"{say_time(at, timezone)}. {CLOSING}")


def on_your_call_line(at: datetime, timezone: str) -> str:
    """After a question she can't answer, with a call booked: where it will be answered."""
    return f"They'll go over it with you in detail on your call on {say_time(at, timezone)}."


# ponytail: any "move"/"change" while a booking is held counts; the read-back catches a misfire.
_RESCHEDULE = re.compile(
    # "change/move/push" only with the booking as its object: "change my CRM" is a question.
    r"\bre-?schedul|\b(?:move|change|push) (?:it|that|this)\b"
    r"|\b(?:move|change|push) (?:the|my|our) (?:call|meeting|appointment|booking|time|date|day)\b"
    r"|\b(?:another|different|other) (?:day|time)\b"
    r"|\b(?:can'?t|cannot|won'?t|not able to) make\b", re.I)
_KEEP = re.compile(r"\bkeep\b|\bthat (?:works|is fine|'s fine)\b|\bstill (?:good|works)\b", re.I)


def wants_reschedule(text: str) -> bool:
    return bool(_RESCHEDULE.search(text))


# "Same time." / "the 20th, same time" -- while moving a booked call: its time of day
# (live 2026-11-16: asked "what time?" three times, then handed off).
_SAME_TIME = re.compile(r"\b(?:the )?same time\b|^\W*(?:the )?same\W*$"
                        r"|\bsame as (?:before|now|it was|the other|my other)\b", re.I)


def means_same_time(text: str) -> bool:
    return bool(_SAME_TIME.search(text))


def keeps_booking(text: str) -> bool:
    return bool(_KEEP.search(text))


def ask_hour_line(day: date) -> str:
    return f"What time would you like on {day:%A}, {day:%B} {day.day}?"


def _clock(hhmm: str) -> str:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return f"{hour % 12 or 12}{f':{minute:02}' if minute else ''} {'AM' if hour < 12 else 'PM'}"


def not_open_line(windows: list[list[str]], timezone: str) -> str:
    """Outside the hours: say what they are (not a slot) and let the caller pick."""
    return (f"I'm sorry, we're not available at that time. That day we're open from "
            f"{_clock(windows[0][0])} to {_clock(windows[-1][1])} "
            f"{_ZONE_NAMES.get(timezone, timezone)} time. What other time would suit you?")


def final_check_line(slot: datetime, timezone: str, email: str | None, *,
                     who: str | None = None, extra: str | None = None) -> str:
    """The one confirmation: every detail read back together -- the time, who it's for, and
    the email asked about outright (owner, 2026-11-17: the booking and the follow-ups go there).
    ``extra``: a second address that gets a copy (live 2026-11-18: "my other mail as well")."""
    when = f"Just to confirm, that's {say_time(slot, timezone)}{f', for {who}' if who else ''}"
    if not email:
        return f"{when}, and our team will send you the booking details. Shall I book it?"
    if extra:
        return (f"{when}. I'll send the booking details to {say_email(email)}, and a copy to "
                f"{say_email(extra)}. Are those emails right?")
    return (f"{when}. I'll send the booking details to {say_email(email)}. Is that the right "
            "email, or would you like me to use a different one?")


# Live 2026-11-18: "send it to my other mail as well" wasn't heard as being about the email
# ("mail", not "email"), so the same final check was read again and again.
_MENTIONS_EMAIL = re.compile(r"\b(?:e-?mails?|mails?)\b|\bmail (?:address|id)\b", re.I)
_ADD_EMAIL = re.compile(r"\b(?:also|as well|too|both|copy|cc|additional|second)\b", re.I)
_REPLACE_EMAIL = re.compile(r"\b(?:instead|different|change|another|other|wrong|new|use)\b",
                            re.I)
_DIFFERENT_ONE = re.compile(r"\b(?:different|another|other|new)\s+(?:one|1)\b", re.I)
EXTRA_EMAIL_ASK = "Sure. What's the other email? Please spell it for me, letter by letter."

# Live call 2026-11-19: "can you once again send me the link" got "I don't have a link to
# share". With a booking it's that booking's confirmation, sent again.
_LINK_AGAIN = re.compile(
    r"\b(?:re-?send|send|e-?mail|forward|share)\b.{0,40}?"
    r"\b(?:link|invite|invitation|calendar invite|confirmation|meeting details|zoom|google meet)\b"
    r"|\b(?:link|invite|invitation|confirmation)\b.{0,40}?\b(?:send|resend)\b.{0,15}?\bagain\b",
    re.I)
_MAILBOXES = {"gmail": "gmail.com", "yahoo": "yahoo.com", "outlook": "outlook.com",
              "hotmail": "hotmail.com", "icloud": "icloud.com"}
LINK_EMAIL_ASK = "Sure. Which email should I send it to? Please spell it for me, letter by letter."
LINK_FAILED_LINE = ("Sorry, I couldn't send it just now, so I've asked our team to email it to "
                    "you.")


def wants_link_again(text: str) -> bool:
    """They want the meeting link / invite / confirmation (again)."""
    return bool(_LINK_AGAIN.search(text))


def named_mailbox(text: str) -> str | None:
    """"to my gmail" -> "gmail.com": which of their addresses they mean."""
    lowered = text.casefold()
    return next((domain for name, domain in _MAILBOXES.items()
                 if re.search(rf"\b{name}\b", lowered)), None)


def link_sent_line(email: str, *, link: bool) -> str:
    what = "the meeting link" if link else "your booking details"
    return f"Done. I've sent {what} again to {say_email(email)}. It should arrive in a minute."
CONFIRM_CLARIFY_LINE = ("Sorry, I didn't quite catch that. Shall I book it, or would you like "
                        "to change the email or the time?")


_DOMAIN = re.compile(r"\bat\s+[a-z0-9-]+(?:\.|\s+dot\s+)(?:com|net|org|co|in|io|us|ai)\b", re.I)


def mentions_domain(text: str) -> bool:
    """ "...send it to it at isngs.com": they're giving an address that didn't come through."""
    return bool(_DOMAIN.search(text))


def email_change(text: str) -> str | None:
    """At the final check: ``"add"`` (a copy to another address too), ``"replace"`` (a
    different address), or ``None`` when they aren't asking about the email."""
    if not _MENTIONS_EMAIL.search(text):
        # "...or would you like to use a different one?" -> "a different one" (Flux writes
        # "1"): the email is what she just asked about (live 2026-11-23).
        return "replace" if _DIFFERENT_ONE.search(text) else None
    if _ADD_EMAIL.search(text):
        return "add"
    return "replace" if _REPLACE_EMAIL.search(text) else None


def who(name: str | None, company: str | None) -> str | None:
    """ "Jane Doe from Lee Roofing" for the final check; ``None`` without a real name."""
    if not name or not all(part.replace("-", "").replace("'", "").isalpha()
                           for part in name.split()):
        return None
    return f"{name} from {company}" if company else name


_CORRECTS_DETAILS = re.compile(r"\b(?:my name|name is|name's|call me|company|business)\b", re.I)


def corrects_details(text: str) -> bool:
    """ "The company is Lee Roofing and Gutters" at the final check: a correction to note."""
    return bool(_CORRECTS_DETAILS.search(text))


def _form_key(label: str) -> str | None:
    key = label.strip().lower()
    for wanted, words in (("service", ("service",)), ("message", ("message", "comment")),
                          ("company", ("company", "business"))):
        if any(w in key for w in words):
            return wanted
    return None


def form_details(notes: list[dict[str, Any]]) -> dict[str, str]:
    """The website form's service, message and company, from the contact's notes (intake
    writes each answer as "label: value"); the latest form wins."""
    found: dict[str, str] = {}
    for note in notes:
        label, sep, value = str(note.get("text", "")).partition(":")
        key = _form_key(label) if sep and note.get("kind") == "need" else None
        if key and value.strip():
            found[key] = value.strip()
    return found


def needs_asking(details: dict[str, str]) -> bool:
    """They picked "Other" and wrote no message: nothing tells Rebecca what they need."""
    return (bool(re.match(r"\s*(?:other|something else)\b", details.get("service", ""), re.I))
            and not details.get("message"))


# -- A form lead: details, then their service (owner, 2026-11-17 / 11-19) -----------------------

DETAILS_FIX_LINE = "Sorry about that. What's your name, and the company you're with?"


def service_words(details: dict[str, str]) -> str | None:
    """The service(s) they picked, as said aloud ("seo, google_ads" -> "SEO and google ads");
    ``None`` for none or "Other" -- that's never named as their need."""
    picked = [p.strip().replace("_", " ") for p in re.split(r"[,;/|]", details.get("service", ""))]
    words = [p.upper() if len(p) <= 3 else p for p in picked
             if p and not re.match(r"(?:other|something else)\b", p, re.I)]
    if not words:
        return None
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def form_hook(details: dict[str, str]) -> str | None:
    """The opening's line about the form when the written one is unusable (live 2026-11-19:
    the generic "Thanks for reaching out" left the form unmentioned)."""
    about = service_words(details)
    return f"Thanks for reaching out about {about}." if about else None


def form_about(details: dict[str, str]) -> str:
    """Leads into her question about their need, once their details are confirmed."""
    about = service_words(details)
    return f"You picked {about} on our form." if about else ""


def details_line(name: str | None, company: str | None) -> str:
    """Right after "yes, now's good": the name and company from the form confirmed, or asked
    for when the form didn't have them."""
    known = who(name, None)
    if known and company:
        return f"Just to make sure I have your details right, you're {known} from {company}?"
    if known:
        return (f"Just to make sure I have your details right, you're {known}? "
                "And which company are you with?")
    if company:
        return f"Could I get your name? I have you down as being with {company}."
    return "Could I get your name, and the company you're with?"


# The lead-in in any case; the name itself capitalised (speech-to-text capitalises names).
_SPOKEN_NAME = re.compile(
    r"\b(?i:my name is|name's|i'm|i am|this is|it's|call me)\s+([A-Z][a-z]+)")
_NOT_NAMES = {"Good", "Fine", "Great", "Here", "Not", "Sure", "Just", "Calling", "Busy", "Okay",
              "Yes", "Yeah", "Well", "The", "From", "With", "Right", "Correct", "Interested"}


def spoken_name(text: str) -> str | None:
    """Their first name from "I'm Kaustubh from GSDS"."""
    match = _SPOKEN_NAME.search(text)
    name = match.group(1) if match else None
    return name if name and name not in _NOT_NAMES else None


def booked_line(slot: datetime, timezone: str, *, emailed: bool = True,
                link: bool = False) -> str:
    when = f"You're booked for {say_time(slot, timezone)}."
    if not emailed:
        return f"{when} Our team will send you the meeting details. {INVITE}"
    if link:
        return (f"{when} I'm sending the meeting link to your email now, and you'll get a "
                f"reminder before the call. {INVITE}")
    return f"{when} You'll get a confirmation email and a reminder before the call. {INVITE}"


# -- What the caller asks for: a day/time, an email address ---------------------------------

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH = "|".join(_MONTHS)
_NUMBERS = {w: str(i) for i, w in enumerate(
    ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
     "twelve"), start=1)}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBERS) + r")\b")
# A number next to a month can be the hour: "4th November 6:30 p.m." is the 4th, not the 6th,
# and "6:30 November 4" isn't the 30th.
_MONTH_DAY = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b"
                        r"(?!:\d|\s*[ap]\.?\s?m\b|\s*o'?clock)")
_DAY_MONTH = re.compile(rf"(?<!:)\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b")
_ORDINAL = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b")
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?|o'?clock)?")
_TIME_WORDS = {"at", "around", "about", "by", "morning", "afternoon", "evening", "night"}


_ZONE_WORDS = {"est": "America/New_York", "edt": "America/New_York",
               "eastern": "America/New_York", "cst": "America/Chicago",
               "cdt": "America/Chicago", "central": "America/Chicago",
               "mst": "America/Denver", "mdt": "America/Denver", "mountain": "America/Denver",
               "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
               "pacific": "America/Los_Angeles", "ist": "Asia/Kolkata",
               "india": "Asia/Kolkata", "indian": "Asia/Kolkata"}
_ZONE_WORD = re.compile(r"\b(" + "|".join(_ZONE_WORDS) + r")\b", re.I)
_ASKS_OPEN = re.compile(r"\b(?:earliest|soonest|available|availability|openings?|whenever"
                        r"|any ?time)\b|\bwhat (?:times?|days?)\b", re.I)


def parse_zone(text: str) -> str | None:
    """The time zone the caller named, if any."""
    m = _ZONE_WORD.search(text)
    return _ZONE_WORDS[m.group(1).lower()] if m else None


def asks_what_is_open(text: str) -> bool:
    return bool(_ASKS_OPEN.search(text))


def _on_or_after(today: date, year: int, month: int, day: int) -> date | None:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    return found if found >= today else None


_UNITS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_UNIT_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
                  "ninth")
_ORDINAL_WORDS = {w: i for i, w in enumerate(_UNIT_ORDINALS, start=1)} | {
    w: i for i, w in enumerate(("tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
                                "fifteenth", "sixteenth", "seventeenth", "eighteenth",
                                "nineteenth", "twentieth"), start=10)} | {"thirtieth": 30} | {
    f"{tens} {w}": base + i for tens, base in (("twenty", 20), ("thirty", 30))
    for i, w in enumerate(_UNIT_ORDINALS, start=1) if base + i <= 31}
_DAY_WORDS = {w: i for i, w in enumerate(("thirteen", "fourteen", "fifteen", "sixteen",
                                          "seventeen", "eighteen", "nineteen", "twenty"),
                                         start=13)} | {"thirty": 30} | {
    f"{tens} {w}": base + i for tens, base in (("twenty", 20), ("thirty", 30))
    for i, w in enumerate(_UNITS, start=1) if base + i <= 31}


def _alternation(words: Any) -> str:
    return "|".join(sorted((w.replace(" ", r"[\s-]+") for w in words), key=len, reverse=True))


_ORD = _alternation(_ORDINAL_WORDS)
_ORDINAL_NEAR_DAY = re.compile(
    rf"(?P<pre>\b(?:{_MONTH})\s+)(?P<a>{_ORD})\b"
    rf"|(?P<the>\bthe\s+)(?P<b>{_ORD})\b"
    r"(?=\s*(?:$|[,.?!]|at\b|of\b|on\b|for\b|works\b|please\b|would\b|is\b))"
    rf"|\b(?P<c>{_ORD})(?P<post>\s+(?:of\s+)?(?:{_MONTH})\b)")
_HOUR_MINUTES = re.compile(
    rf"\b({'|'.join(_NUMBERS)}|\d{{1,2}})[\s-]+(o'?clock|fifteen|thirty|forty[\s-]+five)\b")
_DAY_WORD = re.compile(rf"\b({_alternation(_DAY_WORDS)})\b")
_SPACED_CLOCK = re.compile(r"\b(\d{1,2})\s?(\d{2})(\s*[ap]\.?\s?m\b)")
_MINUTE_WORDS = {"fifteen": "15", "thirty": "30"}


def _spoken_numbers(t: str) -> str:
    """A streaming recogniser writes numbers as words ("fifteenth November, three thirty PM")
    and sometimes drops the colon ("3 30 PM", "330 PM"): turn them into the digits below.
    Ordinal words only count as a day next to a month or after "the" ("the first one" isn't)."""
    def ordinal(m: re.Match[str]) -> str:
        word = m.group("a") or m.group("b") or m.group("c")
        day = f"{_ORDINAL_WORDS[' '.join(word.replace('-', ' ').split())]}th"
        return f"{m.group('pre') or m.group('the') or ''}{day}{m.group('post') or ''}"

    def hour_minutes(m: re.Match[str]) -> str:
        hour = _NUMBERS.get(m.group(1), m.group(1))
        word = " ".join(m.group(2).replace("-", " ").split())
        minutes = "00" if "clock" in word else _MINUTE_WORDS.get(word, "45")
        return f"{hour}:{minutes}"

    t = _ORDINAL_NEAR_DAY.sub(ordinal, t)
    t = _HOUR_MINUTES.sub(hour_minutes, t)
    t = _DAY_WORD.sub(lambda m: str(_DAY_WORDS[" ".join(m.group(1).replace("-", " ").split())]),
                      t)
    return _SPACED_CLOCK.sub(lambda m: f"{m.group(1)}:{m.group(2)}{m.group(3)}", t)


def parse_when(text: str, today: date) -> tuple[date | None, tuple[int, int] | None]:
    """The day and (hour, minute) a caller asked for, each ``None`` when not said.
    Hours 1-7 without am/pm mean the afternoon (nobody books a 3 AM call)."""
    t = _NUMBER_WORD.sub(lambda m: _NUMBERS[m.group(1)], _spoken_numbers(text.lower()))
    day: date | None = None
    if (m := _DAY_MONTH.search(t)) or (m := _MONTH_DAY.search(t)):
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
    r"(?:(?:it'?s|it is|that'?s|the email(?: address)? is|email is"
    r"|my (?:other |second )?(?:e-?mail|mail)(?: address| id)? is"
    r"|(?:you can |please )?use"
    r"|(?:can you |could you |please )?(?:also )?(?:send|mail|email|forward)(?: me)?"
    r"(?: it| them| the (?:booking |meeting )?(?:details|link|invite|confirmation))?"
    r"(?: link)? to(?: my (?:other |second )?(?:e-?mail|mail)(?: address| id)?)?)\s+)?")
# Live 2026-11-18: "don't change anything, send me the booking link to it at isngs.com" became
# the address. Ordinary words in the part before "at" mean it isn't an address yet -- she asks
# for it spelled instead of guessing.
_NOT_EMAIL_WORDS = {
    "send", "me", "the", "to", "it", "is", "my", "other", "mail", "email", "link", "booking",
    "details", "change", "anything", "don't", "dont", "please", "and", "also", "as", "well",
    "meeting", "invite", "address", "id", "copy", "both", "too", "use", "can", "you", "your"}
_EMAIL_SUFFIX = re.compile(r"\s+(?:instead|please|thanks|thank you|ok(?:ay)?)\W*$")
_EMAIL_WORDS = {"dot": ".", "period": ".", "point": ".", "at": "@", "underscore": "_",
                "dash": "-", "hyphen": "-", "zero": "0", "one": "1", "two": "2", "three": "3",
                "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_EMAIL_FILLER = {"capital", "small", "lowercase", "uppercase", "letter"}


def parse_spoken_email(text: str) -> str | None:
    """An email address from what the caller said ("john dot smith at gmail dot com", or spelled
    "j a n e", "J-A-N-E", "m as in mary", "double t"), or ``None`` when it isn't one. Indian
    English "at the rate" is understood too. Owner, 2026-11-17: the booking and every follow-up
    go to it -- the final check reads it back letter by letter before it is used."""
    t = _EMAIL_SUFFIX.sub("", _EMAIL_PREFIX.sub("", text.lower().strip()))
    t = re.sub(r"\bat the rate(?: of)?\b", " at ", t)
    t = re.sub(r"\b([a-z0-9]) as in \w+", r"\1", t)
    t = re.sub(r"\bdouble ([a-z0-9])\b", r"\1\1", t)
    parts: list[str] = []
    for raw in t.split():
        word = raw.strip(",;:!?")
        if word in _EMAIL_FILLER:
            continue
        if re.fullmatch(r"[a-z0-9]\.", word):  # "J. A. N. E." -- spelled letters
            word = word[0]
        elif re.fullmatch(r"[a-z0-9](?:-[a-z0-9])+", word):  # "K-A-U-S"
            word = word.replace("-", "")
        parts.append(_EMAIL_WORDS.get(word, word))
    local = parts[:parts.index("@")] if "@" in parts else parts
    if any(p in _NOT_EMAIL_WORDS for p in local):
        return None
    try:
        return validate_email("".join(parts).strip(".,"),
                              check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


# Said as words; any other domain is spelled out like the rest of the address.
_SPOKEN_DOMAINS = {d: d.replace(".", " dot ") for d in (
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com")}
_SYMBOL_WORDS = {".": "dot", "@": "at", "_": "underscore", "-": "dash"}


def say_email(email: str) -> str:
    """Letter by letter ("J, A, N, E, at, gmail dot com"): a misheard letter is caught at the
    read-back, not in a bounced booking email."""
    local, _, domain = email.partition("@")
    *labels, tld = domain.split(".")
    spoken = [_SYMBOL_WORDS.get(c, c.upper()) for c in local] + ["at"]
    if domain in _SPOKEN_DOMAINS:
        spoken.append(_SPOKEN_DOMAINS[domain])
    else:
        spoken += [_SYMBOL_WORDS.get(c, c.upper()) for c in ".".join(labels)] + ["dot", tld]
    return ", ".join(spoken)


# -- Per-call memory ------------------------------------------------------------------------

Stage = Literal["talking", "discovery", "asking_time", "confirming", "email_ask", "booked_check",
                "qualifying", "email_extra_ask", "link_email_ask", "details_check"]


@dataclass
class CallState:
    stage: Stage = "talking"
    day: str | None = None  # ISO date the caller asked for
    at: list[int] | None = None  # [hour, minute] the caller asked for
    zone: str | None = None  # a time zone they named; else the business's
    chosen: str | None = None  # the open slot (ISO datetime) awaiting the final check
    email: str | None = None  # where the meeting details go
    extra_email: str | None = None  # a second address that gets a copy (2026-11-18)
    email_tries: int = 0
    time_tries: int = 0  # a day/time she didn't catch (never counted as an unanswered question)
    silences: int = 0
    misses: int = 0
    recording_started: bool = False
    question: str | None = None  # asked if they say yes to the opening (written per contact)
    history: list[list[str]] = field(default_factory=list)  # [role, text]
    booked: dict[str, str] | None = None  # their upcoming booking: event_id, at (ISO), tz
    rescheduling: bool = False
    close_offered: bool = False  # the one "before you go, shall I book a call?" was asked
    on_hold: bool = False  # they asked her to hold: silence is waited out quietly
    answered: bool = False  # her last turn answered a question (a pause then gets one follow-up)
    hold_silences: int = 0
    first_name: str | None = None  # used now and then, never in every line
    who: str | None = None  # "Jane Doe from Lee Roofing" for the final check
    # A form lead (owner, 2026-11-19): their name + company, confirmed after "yes, now's good";
    # the question about their need waits for it.
    details: str | None = None
    after_details: str | None = None
    # Plan 1.2: questions she may still ask ([key, wording], at most QUALIFY_PER_CALL), the one
    # awaiting an answer, and what was heard -- mapped to a choice after the call, not during.
    qual_todo: list[list[str]] = field(default_factory=list)
    qual_key: str | None = None
    qual_heard: dict[str, str] = field(default_factory=dict)
    qual_named: bool = False  # her question line already used their first name
    qual_offer: bool = False  # asked after their need: offer a booking once done

    def to_json(self) -> dict[str, Any]:
        return {"stage": self.stage, "day": self.day, "at": self.at, "zone": self.zone,
                "chosen": self.chosen, "email": self.email, "extra_email": self.extra_email,
                "email_tries": self.email_tries, "time_tries": self.time_tries,
                "silences": self.silences, "misses": self.misses,
                "recording_started": self.recording_started, "question": self.question,
                "history": self.history[-12:], "booked": self.booked,
                "rescheduling": self.rescheduling, "close_offered": self.close_offered,
                "on_hold": self.on_hold, "hold_silences": self.hold_silences,
                "answered": self.answered, "first_name": self.first_name, "who": self.who,
                "details": self.details, "after_details": self.after_details,
                "qual_todo": self.qual_todo, "qual_key": self.qual_key,
                "qual_heard": self.qual_heard, "qual_named": self.qual_named,
                "qual_offer": self.qual_offer}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> CallState:
        data = data or {}
        return cls(stage=data.get("stage", "talking"), day=data.get("day"), at=data.get("at"),
                   zone=data.get("zone"), chosen=data.get("chosen"), email=data.get("email"),
                   extra_email=data.get("extra_email"),
                   email_tries=int(data.get("email_tries", 0)),
                   time_tries=int(data.get("time_tries", 0)),
                   silences=int(data.get("silences", 0)),
                   misses=int(data.get("misses", 0)),
                   recording_started=bool(data.get("recording_started", False)),
                   question=data.get("question"),
                   history=[list(h) for h in data.get("history", [])],
                   booked=data.get("booked"),
                   rescheduling=bool(data.get("rescheduling", False)),
                   close_offered=bool(data.get("close_offered", False)),
                   on_hold=bool(data.get("on_hold", False)),
                   hold_silences=int(data.get("hold_silences", 0)),
                   answered=bool(data.get("answered", False)),
                   first_name=data.get("first_name"), who=data.get("who"),
                   details=data.get("details"), after_details=data.get("after_details"),
                   qual_todo=[list(q) for q in data.get("qual_todo", [])],
                   qual_key=data.get("qual_key"),
                   qual_heard=dict(data.get("qual_heard") or {}),
                   qual_named=bool(data.get("qual_named", False)),
                   qual_offer=bool(data.get("qual_offer", False)))
