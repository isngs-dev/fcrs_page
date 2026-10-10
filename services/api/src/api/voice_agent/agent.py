"""AI voice agent -- the "Call Us" button's live phone-style conversation.

Flow: the widget asks ``POST /public/voice-agent/calls`` for a Plivo browser
JWT + a signed call ticket -> the browser dials Plivo (Browser SDK), sending
the ticket as the ``X-PH-Ticket`` header -> Plivo fetches XML from
``/public/voice-agent/answer`` -> we speak the greeting inside ``<GetInput
inputType="speech">`` -> Plivo does the speech-to-text and posts each caller
utterance to ``/public/voice-agent/calls/{call_id}/turn``; ``CallSession``
(persisted on the call row between turns) answers it from the tenant's
knowledge base (same retrieval as the chat) and listens again. When the agent
can't answer, or the caller asks for a person, the turn answers with ``<Dial>``
to the tenant's transfer number at any hour; the agent's own hand-offs
(repeated question, time limit) dial inside business hours only.

Everything here except ``answer_from_knowledge`` is pure, so the transfer
rules are unit-testable without Plivo or an LLM.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger

from api.config import get_api_settings
from api.llm.config_repository import get_llm_config
from api.llm.factory import provider_for
from api.llm.provider import ChatMessage, LLMError
from api.orchestrator.guardrails import scan_output
from api.rag.service import retrieve_hybrid
from api.voice_agent.repository import VoiceAgentConfig

_log = get_logger(__name__)

DEFAULT_GREETING = (
    "Hi, thanks for calling. I'm an AI assistant and I can answer questions about "
    "our services. How can I help you today?"
)
TRANSFER_LINE = "Let me connect you with a member of our team. One moment, please."
AFTER_HOURS_LINE = (
    "Our team isn't available right now. I've opened the booking form in the chat "
    "so you can schedule a call at a time that suits you. Thanks for calling, goodbye."
)
MISS_LINE = f"I'm sorry, I don't have that information. {TRANSFER_LINE}"
GOODBYE_LINE = "Thanks for calling. Have a great day, goodbye."
ERROR_LINE = "Sorry, I'm having trouble on my end."
SILENCE_LINE = "Sorry, I didn't catch that. What can I help you with?"
_MAX_SILENCES = 2  # consecutive turns with no speech -> goodbye

# Reasons recorded on the call row and in the transcript.
TransferReason = Literal[
    "asked_for_person", "could_not_answer", "repeated_question", "time_limit", "agent_error",
]
REASON_TEXT: dict[str, str] = {
    "asked_for_person": "caller asked for a person",
    "could_not_answer": "agent could not answer the question",
    "repeated_question": "caller repeated the same question",
    "time_limit": "call reached the time limit",
    "agent_error": "agent could not reach the knowledge base",
}

_VOICE_SYSTEM_PROMPT = (
    "You are the AI phone assistant for this business, speaking with a caller on "
    "a live call. Answer using ONLY the context below. Speak naturally: 1 to 3 "
    "short sentences, plain words, no lists, no markdown, no links, no emoji. Say "
    "numbers and prices the way a person says them aloud. If the caller only "
    "greets or thanks you, reply briefly and ask how you can help. If the context "
    "does not contain the answer, reply with exactly NO_ANSWER_FOUND and nothing "
    "else. Never guess, never promise, never say anything is booked, and never "
    "invent prices, dates or commitments."
)
_NO_ANSWER = "NO_ANSWER_FOUND"
_HISTORY_TURNS = 6  # recent caller/agent turns sent with each question

_PERSON_RE = re.compile(
    r"\b(?:human|real person|live person|representative|operator)\b"
    r"|\b(?:speak|talk|connect|transfer|put)\b.{0,25}"
    r"\b(?:person|someone|somebody|representative|rep|agent|manager|team|staff)\b",
    re.I,
)
_GOODBYE_RE = re.compile(r"\b(?:bye|goodbye|that's all|that is all|that's it|nothing else)\b", re.I)


_FILLER = {
    "a", "an", "the", "is", "are", "do", "does", "you", "your", "i", "my", "to", "of", "for",
    "um", "uh",
}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower())) - _FILLER


def is_repeat(previous: str | None, current: str) -> bool:
    """The caller asked (nearly) the same thing again -- the last answer didn't help."""
    if previous is None:
        return False
    a, b = _words(previous), _words(current)
    if len(a) < 3 or len(b) < 3:
        return False
    return len(a & b) / len(a | b) >= 0.8


def in_business_hours(config: VoiceAgentConfig, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(config.timezone))
    return (
        local.weekday() in config.open_days
        and config.open_time <= f"{local:%H:%M}" < config.close_time
    )


Action = Literal["answer", "transfer", "goodbye"]


@dataclass
class CallSession:
    """Per-call conversation state + the transfer rules (doc: "When the call is transferred").
    Lives on the call row between Plivo turn webhooks (``to_state``/``from_state``)."""

    max_minutes: int
    started: float = field(default_factory=time.time)  # epoch seconds
    silences: int = 0
    last_prompt: str | None = None
    history: list[ChatMessage] = field(default_factory=list)

    def to_state(self) -> dict[str, Any]:
        return {
            "silences": self.silences,
            "last_prompt": self.last_prompt,
            "history": [[m.role, m.content] for m in self.history],
        }

    @classmethod
    def from_state(
        cls, state: dict[str, Any] | None, *, max_minutes: int, started: float,
    ) -> CallSession:
        state = state or {}
        return cls(
            max_minutes=max_minutes,
            started=started,
            silences=int(state.get("silences", 0)),
            last_prompt=state.get("last_prompt"),
            history=[ChatMessage(role=r, content=c) for r, c in state.get("history", [])],
        )

    def on_silence(self) -> bool:
        """The caller said nothing; ``True`` when it's time to hang up."""
        self.silences += 1
        return self.silences >= _MAX_SILENCES

    def before_answer(self, prompt: str) -> tuple[Action, TransferReason | None]:
        """What to do with a caller utterance before looking anything up."""
        self.silences = 0
        repeat = is_repeat(self.last_prompt, prompt)
        self.last_prompt = prompt
        if _PERSON_RE.search(prompt):
            return "transfer", "asked_for_person"
        if _GOODBYE_RE.search(prompt):
            return "goodbye", None
        if repeat:
            return "transfer", "repeated_question"
        return "answer", None

    def after_answer(self, prompt: str, reply: str | None) -> tuple[str, TransferReason | None]:
        """The line to speak for ``reply`` (``None`` = not in the knowledge base),
        plus a transfer reason when the call should now go to the team. A question
        the knowledge base can't answer goes straight to the team."""
        self.history += [ChatMessage(role="user", content=prompt)]
        if reply is None:
            return MISS_LINE, "could_not_answer"
        self.history.append(ChatMessage(role="assistant", content=reply))
        if time.time() - self.started > self.max_minutes * 60:
            return f"{reply} We've been talking for a while. {TRANSFER_LINE}", "time_limit"
        return reply, None


async def answer_from_knowledge(
    db: Database, claims: AuthClaims, question: str, history: list[ChatMessage],
) -> str | None:
    """A short spoken answer grounded in the tenant's knowledge base, or ``None``
    when the knowledge base doesn't hold it (or the reply fails the output
    guardrails). LLM/retrieval errors propagate -- the caller transfers."""
    config = await get_llm_config(db, claims)
    if config is None or not config.embedding_model:
        raise LLMError("LLM or embeddings not configured for this tenant")
    settings = get_api_settings()
    result = await retrieve_hybrid(db, claims, question, k=settings.orchestrator_rag_k)
    if not result.chunks:
        _log.info(
            "voice agent could not answer",
            extra={"event": "voice_agent_miss", "reason": "no knowledge base matches"},
        )
        return None
    context = "\n".join(f"[{c.chunk_id}] {c.content}" for c in result.chunks)
    prompt = [
        ChatMessage(role="system", content=f"{_VOICE_SYSTEM_PROMPT}\n\nContext:\n{context}"),
        *history[-_HISTORY_TURNS:],
        ChatMessage(role="user", content=question),
    ]
    provider = provider_for(config)
    try:
        # The chat's budget, not a smaller voice cap: reasoning models (gpt-oss)
        # spend part of it thinking, and a tight cap left an empty reply -- every
        # question "missed". The prompt itself keeps the spoken answer short.
        # Low reasoning: the caller is waiting on the line (gpt-oss ~7s -> ~4s).
        completion = await provider.generate(
            prompt, model=config.model, max_tokens=settings.llm_max_tokens,
            reasoning_effort="low",
        )
    finally:
        await provider.aclose()
    reply = completion.text.strip()
    if not reply:
        miss = f"empty reply (stop_reason={completion.stop_reason})"
    elif _NO_ANSWER in reply:
        miss = "not in the knowledge base"
    elif not scan_output(reply).ok:
        miss = "reply failed the output guardrails"
    else:
        return reply
    _log.info("voice agent could not answer", extra={"event": "voice_agent_miss", "reason": miss})
    return None


# -- call ticket ----------------------------------------------------------------


def call_ticket(call_id: str) -> str:
    """``call_id_signature`` -- proves a Plivo webhook belongs to a call our API
    created (the browser sends it as the ``X-PH-Ticket`` SIP header, which only
    allows ``[A-Za-z0-9+-_()]`` -- hence ``_``, not ``.``)."""
    secret = get_api_settings().jwt_secret.encode()
    sig = hmac.new(secret, f"voice-agent:{call_id}".encode(), hashlib.sha256).hexdigest()
    return f"{call_id}_{sig}"


def verify_call_ticket(ticket: str | None) -> str | None:
    """The call_id inside a valid ticket, else ``None``."""
    if not ticket or "_" not in ticket:
        return None
    call_id = ticket.rsplit("_", 1)[0]
    return call_id if hmac.compare_digest(call_ticket(call_id), ticket) else None
