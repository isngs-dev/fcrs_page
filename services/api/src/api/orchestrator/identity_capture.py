"""Conversational identity capture (replaces the SR-14 up-front name+email form).

When a tenant has the identity gate on, the bot asks only for the visitor's
first name on their first message, answers their question by name, and later
in the chat (the ``EMAIL_ASK_TURN``-th substantive turn) asks naturally for an
email so the team can follow up. Anything the visitor types that looks like
an email is captured whenever it appears. Never asks twice for either.

Visitor text only reaches a system prompt as a validated first name
(letters only) -- never raw.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger
from common.phone import to_e164

from api.agents import hooks as agent_hooks
from api.leads.assignment import assign_lead_fail_open
from api.leads.repository import (
    Lead,
    add_activity,
    create_lead,
    get_lead,
    get_lead_id_by_visitor_id,
    update_lead_contact,
)
from api.llm.provider import ChatMessage
from api.notifications.emit import emit_event_safe

_log = get_logger(__name__)

ASK_NAME_INTENT = "ask_name"
ASK_PHONE_INTENT = "ask_phone"
EMAIL_CAPTURED_INTENT = "email_captured"
PHONE_CAPTURED_INTENT = "phone_captured"
EMAIL_ASK_TURN = 3  # the visitor's 3rd substantive turn ends with a natural email ask
PHONE_ASK_TURN = 5  # if the email thank-you didn't already ask for the number
CHAT_EMAIL_CONSENT_VERSION = "chat-2026-10-a"  # see api.agents.consent_wording

ASK_NAME_REPLY = "Happy to help with that! Before we dive in, what's your name?"
# The call-consent disclosure: code-owned and exact (the model never writes it).
# Stored as the consent wording (agents.consent_wording, chat-2026-10-a "call") --
# a number shared right after this is AI-call consent (A1 D5); one shared
# without it is saved but is NOT consent.
PHONE_ASK = ("And what's the best number to reach you? If you share it, our team may call you "
             "about your enquiry, including with our AI assistant, and you can ask us to stop "
             "anytime.")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LEADIN_RE = re.compile(
    r"^(?:hi|hello|hey)?[\s,!.]*"
    r"(?:my name is|my name's|name is|name:|i am|i'm|im|this is|it's|its)?\s*",
    re.I)
_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ .'-]+[^\W\d_]+){0,5}$")  # <= 3 words checked below
# Short replies that are not names (answers to "what's your name?" that dodge it).
_NOT_NAMES = {
    "no", "nope", "nah", "yes", "yeah", "ok", "okay", "sure", "why", "skip", "later", "none",
    "anonymous", "pass", "hi", "hello", "hey", "thanks", "thank", "you", "price", "pricing",
    "cost", "quote", "roof", "repair", "help", "question", "what", "how", "when", "where",
    "rather", "not", "dont", "don't", "prefer", "say", "tell", "no thanks",
}


def name_from_reply(message: str) -> str | None:
    """A first/last name typed in answer to "what's your name?", or ``None``.

    ponytail: heuristic (short, letters only, not a known dodge word); a longer
    sentence or a question is treated as "didn't give a name" and just answered.
    """
    text = message.strip().strip(".!")
    if "?" in text or len(text) > 60:
        return None
    text = _LEADIN_RE.sub("", text).strip(" ,.!")
    if not text or not _NAME_RE.match(text):
        return None
    words = {w.lower() for w in re.split(r"[ .'-]+", text) if w}
    if words & _NOT_NAMES or len(text.split()) > 3:
        return None
    return " ".join(w[:1].upper() + w[1:] for w in text.split())


def safe_first_name(name: str | None) -> str | None:
    """The first name, only if it's plain letters (safe to put in a prompt)."""
    if not name:
        return None
    first = name.strip().split()[0] if name.strip() else ""
    return first if re.fullmatch(r"[^\W\d_](?:[^\W\d_]|['-]){0,29}", first) else None


def email_in(message: str) -> str | None:
    match = _EMAIL_RE.search(message)
    return match.group(0).strip(".").lower() if match else None


def is_only_email(message: str, email: str) -> bool:
    """The message is just the email (maybe with a few filler words)."""
    rest = _EMAIL_RE.sub("", message).strip(" .,!-:\n")
    return len(rest.split()) <= 4 and "?" not in rest


def email_ack_reply(first_name: str | None, email: str, *, ask_phone: bool = False) -> str:
    who = f", {first_name}" if first_name else ""
    ack = f"Thanks{who}! I've noted {email} so our team can follow up with you."
    return f"{ack} {PHONE_ASK}" if ask_phone else f"{ack} What else can I help you with?"


_PHONE_RE = re.compile(r"\+?\(?\d[\d\s().-]{8,}\d")


def phone_in(message: str) -> str | None:
    """A dialable phone number (E.164) in the visitor's text, or ``None``."""
    for candidate in reversed(_PHONE_RE.findall(_EMAIL_RE.sub(" ", message))):
        e164 = to_e164(candidate)
        if e164 is not None:
            return e164
    return None


def is_only_phone(message: str) -> bool:
    rest = _PHONE_RE.sub("", _EMAIL_RE.sub("", message)).strip(" .,!-:\n")
    return len(rest.split()) <= 4 and "?" not in rest


def phone_ack_reply(first_name: str | None, *, consented: bool) -> str:
    who = f", {first_name}" if first_name else ""
    if consented:
        return f"Thanks{who}! Our team will be in touch. What else can I help you with?"
    return f"Thanks{who}, got it! What else can I help you with?"


async def visitor_lead(db: Database, claims: AuthClaims) -> Lead | None:
    lead_id = await get_lead_id_by_visitor_id(db, claims, claims.subject)
    return await get_lead(db, claims, lead_id) if lead_id else None


async def _save(db: Database, claims: AuthClaims, lead: Lead | None, *, name: str | None,
                email: str | None, purpose: str, text: str,
                phone: str | None = None) -> Lead | None:
    now = datetime.now(UTC).isoformat()
    consent: dict[str, Any] = {**(lead.consent if lead else {}), "granted": True,
                               "purpose": purpose, "text": text, "captured_at": now,
                               f"{purpose}_captured_at": now}
    if lead is None:
        lead_id = await create_lead(db, claims, visitor_id=claims.subject, name=name,
                                    email=email, phone=phone, consent=consent,
                                    source="chat_identity")
        await assign_lead_fail_open(db, claims, lead_id=lead_id)
        await emit_event_safe(db, claims, kind="lead_captured", category="leads",
                              target_type="lead", target_id=lead_id,
                              payload={"lead_id": lead_id}, actor_id=None)
    else:
        lead_id = lead.lead_id
        await update_lead_contact(db, claims, lead_id, name=name, email=email, consent=consent,
                                  phone=phone)
    field = "email" if email else "phone" if phone else "name"
    await add_activity(db, claims, lead_id, type="identified_in_chat",
                       payload={"field": field}, actor="visitor")
    return await get_lead(db, claims, lead_id)


async def save_name(db: Database, claims: AuthClaims, lead: Lead | None, name: str
                    ) -> Lead | None:
    return await _save(db, claims, lead, name=name, email=None, purpose="chat_name",
                       text=ASK_NAME_REPLY)


async def save_email(db: Database, claims: AuthClaims, lead: Lead | None, email: str
                     ) -> Lead | None:
    """Store the email and hand it to the agents as email follow-up consent (A4)."""
    updated = await _save(
        db, claims, lead, name=None, email=email, purpose="chat_email",
        text="The visitor typed their email in the chat after being asked for the best "
             "email so the team could follow up.")
    if updated is not None:
        await agent_hooks.inbound_lead_created(
            db, claims, lead_id=updated.lead_id, channels=["email"],
            version=CHAT_EMAIL_CONSENT_VERSION, timezone=None, form="chat")
    return updated


async def save_phone(db: Database, claims: AuthClaims, lead: Lead | None, phone: str, *,
                     consented: bool, timezone: str | None) -> Lead | None:
    """Store the number. Only right after ``PHONE_ASK`` is it AI-call consent for the
    agents (A4 hook, ``call`` channel, with the browser timezone for call windows)."""
    text = PHONE_ASK if consented else "The visitor typed a phone number in the chat."
    updated = await _save(db, claims, lead, name=None, email=None, phone=phone,
                          purpose="chat_phone", text=text)
    if updated is not None and consented:
        await agent_hooks.inbound_lead_created(
            db, claims, lead_id=updated.lead_id, channels=["call"],
            version=CHAT_EMAIL_CONSENT_VERSION, timezone=timezone, form="chat")
    return updated


def deferred_question(messages: list[Any]) -> str | None:
    """The visitor message the name question interrupted (just before our ask)."""
    for i in range(len(messages) - 1, 0, -1):
        if messages[i].role == "bot" and messages[i].intent == ASK_NAME_INTENT:
            prior = messages[i - 1]
            return str(prior.content) if prior.role == "user" else None
    return None


def personalize(prompt: list[ChatMessage], *, first_name: str | None,
                deferred: str | None, ask_email: bool) -> list[ChatMessage]:
    """Add name/email-ask guidance to the system message; re-ask the deferred question."""
    notes = []
    if first_name:
        notes.append(f"The visitor's first name is {first_name}. Use it naturally now and "
                     "then (not in every message).")
    if ask_email:
        notes.append(
            "After answering, end with one short, friendly sentence asking for the best "
            "email to reach them so the team can follow up. Keep it natural and optional; "
            "don't ask for anything else.")
    if not notes and not deferred:
        return prompt
    out = list(prompt)
    if notes and out and out[0].role == "system":
        out[0] = ChatMessage(role="system", content=out[0].content + "\n\n" + " ".join(notes))
    if deferred and out and out[-1].role == "user":
        intro = f"My name is {first_name}. " if first_name else ""
        out[-1] = ChatMessage(role="user", content=f"{intro}{deferred}")
    return out
