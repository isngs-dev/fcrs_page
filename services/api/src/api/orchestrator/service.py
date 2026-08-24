"""Orchestrator turn pipeline -- the brain's judgement layer (S10.2, S10.5).

``answer_turn`` composes the modules S2-S6 already shipped, plus S10.2's own
intent gate + 3-way decision, into one turn: resolve the tenant's LLM config,
resolve the tenant's orchestrator thresholds, get-or-create the conversation,
store the user turn, classify intent, branch on intent (chit-chat -> direct
answer, scheduling_request/off_topic -> escalate, question/other -> grounded
RAG + confidence-band decision), store the assistant turn, and return the
answer + decision + sources. No new RAG/LLM/store *mechanisms* -- this module
only composes their existing public functions, always with the caller's own
VISITOR ``claims``.

No silent fallback (CLAUDE.md §3): a misconfigured tenant fails before any
store write (decision 9); a runtime classify/RAG/LLM failure fails loud AFTER
the user turn is durably stored -- never a fabricated answer, never data
loss. The 3-way decision (``answer``/``clarify``/``escalate``) is a PURE
function of the closed-set intent label + the numeric confidence vs the
tenant's two thresholds -- no fuzzy LLM-decided branch selection. This
SUPERSEDES the S10.1 amendment's standalone ``orchestrator_confidence_floor``
short-circuit and ``_LOW_CONFIDENCE_REPLY`` -- "below floor" is now simply
``confidence < cfg.escalate_threshold`` inside the unified decision.

S10.5 splits the pipeline at the LLM call (decision 1): ``_resolve_turn``
runs everything BEFORE a ``generate``/``stream`` call and returns a
discriminated plan (``_ReplayOutcome`` | ``_FixedOutcome`` | ``_GeneratePlan``);
``_finalize_generation`` is the single home of the post-generate guardrail
scan + degrade logic, shared by both the non-streaming ``answer_turn`` and
the streaming ``answer_turn_stream``. This keeps the two delivery modes from
ever drifting -- a turn-cap or guardrail fix is made once, not twice.

Post-generation no-answer override: a grounded generation carrying the
``_NO_ANSWER_SENTINEL`` clarifies only on turns 1-2 when the prior assistant
decision was not ``"clarify"``; every other no-answer escalates at this same
seam. The pre-generation ``_decide()`` confidence band remains pure and
untouched; this is a second deterministic post-generation check alongside
guardrails, not a revival of the S10.1 free-text confidence floor.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError
from common.logging import get_logger

from api.config import get_api_settings
from api.conversation_store.repository import (
    append_message,
    count_messages,
    create_conversation,
    get_last_assistant_decision,
    get_message,
    get_recent_assistant_decisions,
    get_working_memory,
)
from api.leads.repository import get_lead, get_lead_id_by_visitor_id
from api.llm.config_repository import get_llm_config
from api.llm.factory import provider_for
from api.llm.provider import ChatMessage, LLMError, LLMProvider
from api.orchestrator.config_repository import OrchestratorConfig, get_orchestrator_config
from api.orchestrator.guardrails import scan_output
from api.rag.service import HybridMatch, retrieve_hybrid
from api.scheduling.repository import get_availability

_log = get_logger(__name__)

Decision = Literal["answer", "clarify", "escalate", "blocked"]


@dataclass(frozen=True)
class Source:
    """A cited chunk, surfaced to the caller -- identifiers + score only."""

    doc_id: str
    chunk_id: str
    score: float | None
    matched_by: list[str]


@dataclass(frozen=True)
class TurnResult:
    """The outcome of one turn -- returned by ``answer_turn``.

    ``intent`` and ``guardrail_flag`` are carried for the route's PII-safe log
    line only (closed-set, non-PII labels) -- ``ChatMessageResponse`` does not
    surface them (decision 10/S10.3 decision 6: stored for analytics, not
    part of the public response). ``action`` (S10.3) IS surfaced -- it tells
    the widget whether to render the consent-gated lead form.
    """

    conversation_id: str
    message_id: str
    reply: str
    decision: str
    confidence: float | None
    sources: list[Source]
    intent: str | None = None
    action: str | None = None
    guardrail_flag: str | None = None


# -- S10.5 decision 1: the discriminated plan returned by ``_resolve_turn`` --
# everything BEFORE a generate/stream call is single-sourced here; both
# ``answer_turn`` and ``answer_turn_stream`` call it and branch on the type.


@dataclass(frozen=True)
class _ReplayOutcome:
    """A step-4 idempotent-replay hit -- the stored reply, returned verbatim.

    No generation, no stream, no store (the row already exists).
    """

    conversation_id: str
    message_id: str
    reply: str
    decision: str
    confidence: float | None
    sources: list[Source]
    intent: str | None
    action: str | None
    guardrail_flag: str | None


@dataclass(frozen=True)
class _FixedOutcome:
    """A fully-decided, no-generation turn -- the reply is a trusted constant.

    Covers the turn-cap escalate, the ``off_topic``/``scheduling_request``
    escalate, ``clarify``, and the sub-floor-confidence escalate. Never
    scanned by ``scan_output`` (S10.3 decision 4) -- these are our own
    literals, not model output.
    """

    conversation_id: str
    assistant_id: str | None
    reply: str
    decision: str
    confidence: float | None
    sources: list[Source]
    intent: str | None
    action: str | None
    grounded: bool
    tokens: int | None


@dataclass(frozen=True)
class _GeneratePlan:
    """The two branches that call the LLM: grounded ``answer`` and ``chitchat``.

    ``grounded`` distinguishes them (True = grounded-answer -> real
    ``sources``/``confidence``; False = chit-chat -> ``sources=[]``/
    ``confidence=None``). ``provider`` is carried alongside ``model`` so the
    caller never re-resolves the tenant's LLM config or re-classifies.
    """

    conversation_id: str
    assistant_id: str | None
    prompt: list[ChatMessage]
    grounded: bool
    decision: str
    confidence: float | None
    sources: list[Source]
    intent: str | None
    model: str
    provider: LLMProvider
    turns: int
    prev_decision: str | None


@dataclass(frozen=True)
class _FinalizedGeneration:
    """The result of ``_finalize_generation`` -- the field bundle the caller
    stores + returns. ``tokens`` is deliberately NOT included here (decision
    8: streamed turns store ``tokens=None``, non-streaming stores the real
    ``output_tokens`` -- the caller supplies that itself)."""

    reply: str
    decision: str
    sources: list[Source]
    grounded: bool
    action: str | None
    guardrail_flag: str | None
    resolve_escalate_action: bool = False


# -- Intent taxonomy (decision 1) -- a fixed, code-owned label set. Only the
# numeric thresholds are tenant-tunable; the taxonomy itself is not.
_INTENT_LABELS: list[str] = ["question", "chitchat", "scheduling_request", "off_topic", "other"]

# Bare label names alone under-specify "off_topic" vs "question": a
# grammatically well-formed question unrelated to this business (e.g. "What
# is the capital of France?" or "Who is the President of India?") has no
# signal distinguishing it from a genuine on-topic "question" without this.
# Passed to `provider.classify()`'s `label_descriptions` (classify_matching.py)
# so the classify prompt states each label's MEANING, not just its name --
# still code-owned/domain-agnostic (no real tenant/business facts), so this
# stays consistent across every tenant regardless of what they sell.
_INTENT_LABEL_DESCRIPTIONS: dict[str, str] = {
    "question": (
        "a genuine question about THIS business -- its products, services, "
        "pricing, policies, hours, or how to get help from it"
    ),
    "chitchat": "a greeting, pleasantry, or small talk that is not asking anything specific",
    "scheduling_request": (
        "an explicit request to book a call, schedule a meeting, or talk to a person/sales rep"
    ),
    "off_topic": (
        "unrelated to this business -- general knowledge trivia, current "
        "events, other companies, public figures, politics, or any topic "
        "this business's assistant would not be expected to answer"
    ),
    "other": "does not clearly fit any of the above",
}

_NO_ANSWER_SENTINEL = "NO_ANSWER_FOUND"
# SR-13 decision 3: an early no-answer clarifies at most once.
_NO_ANSWER_CLARIFY_MAX_TURNS = 2

_FORMATTING_RULES = (
    " Formatting rules: write short plain paragraphs. You may use **bold**, "
    "*italic*, and `inline code`. Never use markdown tables, bullet points "
    "(- or *), headings (#), blockquotes (>), or numbered-list markdown; when "
    "listing or comparing options, write them as short sentences or as plain "
    'lines like "1) ..." on separate lines. Keep the whole reply concise -- '
    "roughly 3-5 sentences, and well under 1000 characters. Cover only what "
    "directly answers the question; do not restate the question, pad with "
    "filler, or add generic caveats. Still give a specific, useful answer -- "
    "do not cut real substance just to hit a shorter length."
)

_GROUNDING_SYSTEM_PROMPT = (
    "You are a helpful assistant for this business. Answer the user's "
    "question using ONLY the context provided below. If the context does "
    "not contain the answer, reply with exactly `NO_ANSWER_FOUND` and nothing "
    "else -- do not apologize, do not invent facts, prices, or commitments."
    + _FORMATTING_RULES
)

_NO_CONTEXT_LINE = "(no relevant knowledge was found)"

# Chit-chat branch: a brief, ungrounded reply -- NO RAG context block. The
# model must not answer business-specific questions from this prompt; it
# should redirect the visitor to ask about the business instead.
_CHITCHAT_SYSTEM_PROMPT = (
    "Reply briefly and warmly. Do NOT answer business-specific questions or "
    "invent facts, prices, or commitments; if asked something specific, "
    "invite them to ask about the business."
    + _FORMATTING_RULES
)

# Fixed clarify template (decision 6) -- deterministic, cheap, no generate
# call; sidesteps the weak-model self-censor gap that motivated the S10.1
# amendment (we do not re-trust the model to gracefully decline).
_CLARIFY_REPLY = (
    "Could you tell me a bit more about what you're looking for? A few "
    "extra details will help me find the right answer."
)

# Fixed escalate template (S10.4 decision 7) -- scheduling-forward AND
# consent-forward, phrased to work under EITHER action outcome
# (schedule_cta/lead_form -- decision 5 makes the choice conditional on
# tenant availability, so the copy itself never hard-commits to "book a
# call" being the only path). The widget renders the S8.1 booking UI or the
# S7.1 lead form depending on which `action` the turn carries.
_ESCALATE_REPLY = (
    "I'd like to respond more precisely - let's get you a proper answer "
    "from someone on the team. Book a call if that's available, or share "
    "your name and email below and I'll make sure it gets to the right "
    "person. Happy to help with anything else in the meantime, too."
)

# Fixed off-topic template. Kept as its own constant so a future divergence
# from _ESCALATE_REPLY stays a one-line change, but currently set to the
# SAME wording on explicit user request (both used to read differently --
# off_topic as a scope-mismatch message, scheduling_request/sub-floor as a
# not-confident-enough one -- but the visitor-facing voice should be
# consistent regardless of which internal reason triggered the fallback).
_OFF_TOPIC_REPLY = _ESCALATE_REPLY

# Fixed turn-cap template (S10.4 decision 7; retuned once the reply text
# actually reaches the screen instead of being overwritten by the widget's
# now-removed hardcoded apology). Same dual-purpose (schedule_cta-or-
# lead_form) phrasing as _ESCALATE_REPLY, but acknowledges the long
# conversation and drops the earlier "if that's available" hedge -- the
# widget already renders whichever card the `action` actually resolves to
# (calendar or lead form) directly beneath this reply, so the copy no longer
# needs to hedge its own uncertainty; it still never hard-promises a
# calendar (dual-purpose, per decision 7 -- the fallback may still resolve
# to lead_form).
_TURN_CAP_REPLY = (
    "We've covered a lot here -- let's connect you with someone from our "
    "team who can help further. Book a call, or share your name and email "
    "below and confirm you're happy for us to contact you, and we'll reach "
    "out."
)

# Fixed repeated-low-confidence template -- distinct from _ESCALATE_REPLY:
# fires when the last `cfg.low_confidence_streak_cap` assistant turns in a
# row already landed in clarify/escalate (never a real "answer"), so another
# _CLARIFY_REPLY would just repeat the same non-answer a third+ time. Warm,
# not apologetic-to-the-point-of-unhelpful, and explicitly invites the
# visitor to keep asking about anything else -- this is a per-topic
# escalation, not a conversation-ending one; the very next turn still runs
# the full pipeline normally and can answer confidently if it's a different,
# well-covered question. Same dual-purpose schedule_cta/lead_form phrasing.
_REPEATED_LOW_CONFIDENCE_REPLY = (
    "I don't want to keep guessing on this one -- let's get you a proper "
    "answer from someone on the team. Book a call if that's available, or "
    "share your name and email below and I'll make sure it gets to the "
    "right person. Happy to help with anything else in the meantime, too."
)

# Fixed identity-gate template (SR-14 D1/D4) -- the trusted-constant reply
# shown on a gated tenant's first bot reply (and every subsequent reply,
# D2's absolute gate) when the visitor has no captured identity yet. The
# widget renders the consent-gated <IdentityForm> inline in the thread on
# action="identity_form"; the visitor's real question is already durable
# (step 5 ran before this branch, C2) and is auto-answered on capture (D3).
_IDENTITY_GATE_REPLY = (
    "Before I answer, could you share your name and email? I'll use it to "
    "follow up on this conversation -- once you've confirmed, I'll answer "
    "your question right away."
)

# Fixed guardrail-block safe reply (S10.3 decision 4) -- substituted for a
# generated reply that trips scan_output. Never the flagged text; also
# consent-forward (same downstream UX as a genuine escalate -- offer a human,
# honestly).
_GUARDRAIL_SAFE_REPLY = (
    "Sorry, I can't help with that here. If you'd like, share your name and "
    "email below and confirm you're happy to be contacted, and I'll connect "
    "you with someone who can."
)

# Maps the store's role column ('user'|'bot'|'system') to the LLM provider's
# expected chat role ('user'|'assistant'|'system') -- the store never sends
# role="bot" to a provider (S4.1 decision 5 vs S10.1 decision 4).
_ROLE_MAP: dict[str, str] = {"user": "user", "bot": "assistant", "system": "system"}


def _decide(confidence: float, cfg: OrchestratorConfig) -> Decision:
    """The pure confidence-band function (decision 2) for the grounded path.

    ``confidence >= cfg.answer_threshold`` -> ``answer``;
    ``cfg.escalate_threshold <= confidence < cfg.answer_threshold`` ->
    ``clarify``; ``confidence < cfg.escalate_threshold`` -> ``escalate``.
    When ``cfg.escalate_threshold == cfg.answer_threshold`` the clarify band
    collapses -- only ``answer``/``escalate`` are ever returned.
    """
    if confidence >= cfg.answer_threshold:
        return "answer"
    if confidence >= cfg.escalate_threshold:
        return "clarify"
    return "escalate"


async def _has_captured_identity(db: Database, claims: AuthClaims) -> bool:
    """Has this visitor already captured identity in this tenant? (SR-14 D1/D2)

    Reuses SR-9.1's exact create-or-link lookup (``get_lead_id_by_visitor_id``)
    rather than inventing a new query -- the visitor's most-recent lead,
    tenant-scoped. A visitor with NO lead at all has not captured identity.
    A visitor WITH a lead may still have NULL ``name``/``email`` (migration
    0039 -- an anonymous SR-9.1 booking creates a NULL-contact lead); that
    case is also "not yet captured" so the gate still fires and D6's
    ``update_lead_contact`` fills in the existing row rather than a fresh
    lead being created. Only a lead with BOTH ``name`` and ``email`` set
    counts as captured -- the never-re-ask half of D2.
    """
    lead_id = await get_lead_id_by_visitor_id(db, claims, claims.subject)
    if lead_id is None:
        return False
    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        return False
    return lead.name is not None and lead.email is not None


async def _schedule_action(db: Database, claims: AuthClaims) -> str:
    """Resolve the CTA action for an ``escalate`` turn (S10.4 decision 5).

    The ONE sanctioned read-only cross-module call into
    ``api/scheduling/**``: a single-row, tenant-scoped ``get_availability``
    read. Returns ``"schedule_cta"`` when the tenant has availability
    configured (a row exists), else ``"lead_form"`` -- never fetches slots,
    never creates events. Called from every ``escalate`` branch (turn-cap,
    off_topic/scheduling_request, sub-floor-confidence); NEVER called from a
    ``blocked`` branch (decision 4 -- that stays a flat, unconditional
    ``"lead_form"``).
    """
    avail = await get_availability(db, claims)
    return "schedule_cta" if avail is not None else "lead_form"


def _build_prompt(wm: dict[str, Any], chunks: list[HybridMatch]) -> list[ChatMessage]:
    """Build ``[system, ...history]`` for the grounded ``generate`` call.

    The system message carries the grounding instruction, a context block
    (one ``[{chunk_id}] {content}`` line per chunk, or the literal
    "no relevant knowledge was found" line when ``chunks`` is empty), and an
    optional "Conversation summary so far" section. The remaining messages
    are ``wm["messages"]`` role-mapped via ``_ROLE_MAP`` -- the last one is
    the current question (already appended to the store), so it is not
    re-appended separately.
    """
    if chunks:
        context_block = "\n".join(f"[{c.chunk_id}] {c.content}" for c in chunks)
    else:
        context_block = _NO_CONTEXT_LINE

    system_parts = [_GROUNDING_SYSTEM_PROMPT, "", "Context:", context_block]
    summary = wm.get("summary")
    if summary:
        system_parts.append("")
        system_parts.append(f"Conversation summary so far: {summary}")

    prompt = [ChatMessage(role="system", content="\n".join(system_parts))]
    for m in wm["messages"]:
        prompt.append(ChatMessage(role=_ROLE_MAP.get(m.role, m.role), content=m.content))
    return prompt


def _build_chitchat_prompt(wm: dict[str, Any]) -> list[ChatMessage]:
    """Build ``[system, ...history]`` for the chit-chat ``generate`` call --
    no RAG context block, a dedicated brief/warm system prompt."""
    prompt = [ChatMessage(role="system", content=_CHITCHAT_SYSTEM_PROMPT)]
    for m in wm["messages"]:
        prompt.append(ChatMessage(role=_ROLE_MAP.get(m.role, m.role), content=m.content))
    return prompt


def _sources_from_chunks(chunks: list[HybridMatch]) -> list[Source]:
    return [
        Source(doc_id=c.doc_id, chunk_id=c.chunk_id, score=c.score, matched_by=c.matched_by)
        for c in chunks
    ]


def _sources_to_payload(sources: list[Source]) -> list[dict[str, Any]]:
    return [
        {
            "doc_id": s.doc_id,
            "chunk_id": s.chunk_id,
            "score": s.score,
            "matched_by": s.matched_by,
        }
        for s in sources
    ]


async def _resolve_turn(
    db: Database,
    claims: AuthClaims,
    *,
    message: str,
    conversation_id: str | None,
    message_id: str | None,
) -> _ReplayOutcome | _FixedOutcome | _GeneratePlan:
    """Run steps 1-8 of the turn pipeline (S10.5 decision 1) -- everything
    BEFORE a ``generate``/``stream`` call. Single-sourced by both
    ``answer_turn`` and ``answer_turn_stream`` so the two delivery modes
    cannot drift.

    Raises
    ------
    ValidationError (``LLM_NOT_CONFIGURED``)
        No LLM config for this tenant -- raised BEFORE any store write.
    ValidationError (``RAG_EMBEDDING_NOT_CONFIGURED``)
        Propagates from ``retrieve_hybrid`` -- AFTER the user turn is stored.
    NotFoundError (``CONVERSATION_NOT_FOUND``)
        A supplied ``conversation_id`` is not visible to this visitor.
    LLMError
        Propagates untouched from ``classify`` (via ``retrieve_hybrid``'s
        ``embed`` too) -- the user turn is preserved; nothing is stored on a
        pre-generation failure beyond the already-durable user turn.
    """
    settings = get_api_settings()

    # Step 1: resolve the tenant's LLM config up front -- fail fast BEFORE
    # any store write (deterministic misconfiguration).
    config = await get_llm_config(db, claims)
    if config is None:
        raise ValidationError(
            "LLM is not configured for this tenant.",
            code="LLM_NOT_CONFIGURED",
        )

    # Step 2: resolve the tenant's orchestrator thresholds -- read-only,
    # tenant-scoped, no side effects. Never None (get-or-default).
    cfg = await get_orchestrator_config(db, claims)

    # Step 3: get-or-create the conversation.
    client_supplied_conversation_id = conversation_id is not None
    if conversation_id is None:
        conversation_id = await create_conversation(db, claims)

    # Step 4: turn-idempotency replay check -- only when the CLIENT supplied
    # BOTH message_id and conversation_id (a first-turn retry can't dedup;
    # the client never received the server-generated conversation_id yet).
    # On a hit, classify/retrieve_hybrid/generate are ALL skipped and the
    # stored decision/reply/confidence/sources are returned verbatim.
    assistant_id: str | None = None
    if message_id is not None:
        assistant_id = f"{message_id}-a"
        if client_supplied_conversation_id:
            existing = await get_message(db, claims, conversation_id, assistant_id)
            if existing is not None:
                existing_decision = existing.decision or "answer"
                return _ReplayOutcome(
                    conversation_id=conversation_id,
                    message_id=existing.message_id,
                    reply=existing.content,
                    decision=existing_decision,
                    confidence=existing.confidence,
                    sources=[Source(**s) for s in (existing.sources or [])],
                    intent=existing.intent,
                    action=existing.action,
                    guardrail_flag=existing.guardrail_flag,
                )

    # Step 5: store the user turn BEFORE classify/RAG/LLM -- idempotent
    # (ON CONFLICT DO NOTHING); a later 5xx never loses the visitor's message.
    await append_message(
        db, claims, conversation_id, role="user", content=message, message_id=message_id,
    )

    provider = provider_for(config)

    # Step 5.5 (SR-14 D1/D2/D9): the identity gate -- an INDEPENDENT,
    # pre-empting trigger, placed AFTER the user turn is durably stored (C2)
    # and BEFORE the turn-cap check. Fires when the tenant has the gate
    # enabled (default OFF, D9) AND this visitor has no captured identity yet
    # (D1's "no server-side conversation at widget-open" reconciliation --
    # this is the earliest point the server can gate). Absolute (D2): every
    # subsequent ungated message returns this same reply again, forever, until
    # captured -- no escape hatch, no turn budget spent trying. Zero LLM cost
    # (C1): classify/retrieve_hybrid/generate never run on a gate turn -- the
    # provider was resolved but never used, so it is closed here exactly like
    # the turn-cap short-circuit below.
    if cfg.identity_gate_enabled and not await _has_captured_identity(db, claims):
        await provider.aclose()
        return _FixedOutcome(
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            reply=_IDENTITY_GATE_REPLY,
            decision="identity_gate",
            confidence=None,
            sources=[],
            intent=None,
            action="identity_form",
            grounded=False,
            tokens=None,
        )

    # Step 6 (S10.4 decision 1): the turn-count cap -- an INDEPENDENT,
    # pre-empting trigger, counted AFTER the user turn is stored, strict `>`.
    # Counts this conversation's visitor turns (role="user"), including the
    # one just stored, MINUS any identity-gate turns (SR-14 D10): each gate
    # bot reply (decision="identity_gate") corresponds 1:1 to a visitor turn
    # that was never substantively answered, so it must not consume any of
    # the visitor's limited turn_cap budget -- otherwise D3's deferred
    # re-send would double-count the same original question. When capped,
    # classify/retrieve_hybrid/generate are skipped entirely (no LLM cost) --
    # a deterministic, honest 200 escalate.
    raw_turns = await count_messages(db, claims, conversation_id, role="user")
    gate_turns = await count_messages(
        db, claims, conversation_id, role="bot", decision="identity_gate",
    )
    turns = raw_turns - gate_turns

    if turns > cfg.turn_cap:
        action = await _schedule_action(db, claims)
        # Turn-cap short-circuits before classify -- provider was resolved
        # but never used; close it here since no _GeneratePlan will carry it
        # onward for the caller to close.
        await provider.aclose()
        return _FixedOutcome(
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            reply=_TURN_CAP_REPLY,
            decision="escalate",
            confidence=None,
            sources=[],
            intent=None,
            action=action,
            grounded=False,
            tokens=None,
        )

    # Step 7: classify intent -- runs AFTER the user turn is durably
    # stored, so a classify failure never loses the visitor's message. On
    # LLMError this propagates untouched -> LLM_ERROR 502 (decision 9,
    # fail-loud, same class of failure as generate).
    # `label_descriptions` tells the model what each label MEANS (not just
    # its bare name) so a well-formed but off-topic question ("What's the
    # capital of France?") is reliably distinguished from an on-topic one --
    # see _INTENT_LABEL_DESCRIPTIONS's own comment.
    intent = await provider.classify(
        message, _INTENT_LABELS, model=config.model, label_descriptions=_INTENT_LABEL_DESCRIPTIONS,
    )

    # Step 8: branch on intent -> decision -> plan (decisions 2 + 6).
    if intent == "chitchat":
        wm = await get_working_memory(
            db, claims, conversation_id, keep_recent=settings.orchestrator_history_turns,
        )
        prompt = _build_chitchat_prompt(wm)
        return _GeneratePlan(
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            prompt=prompt,
            grounded=False,
            decision="answer",
            confidence=None,
            sources=[],
            intent=intent,
            model=config.model,
            provider=provider,
            turns=turns,
            prev_decision=None,
        )

    if intent in ("scheduling_request", "off_topic"):
        # No RAG, no generate -- a fixed, honest, trusted-constant reply.
        # Fixed-template branches are never scanned (decision 4).
        # S10.4 decision 4/5: escalate -> schedule_cta when the tenant
        # has availability configured, else lead_form (the ONE read-only
        # api/scheduling/** check the orchestrator ever makes).
        action = await _schedule_action(db, claims)
        # classify already ran on `provider`; no _GeneratePlan will carry it
        # onward, so close it before returning the fixed-template reply.
        await provider.aclose()
        # off_topic gets its own honest copy ("outside what I can help with")
        # -- distinct from scheduling_request's _ESCALATE_REPLY, which fits a
        # "wants to book/talk to someone" request, not a scope mismatch.
        reply = _OFF_TOPIC_REPLY if intent == "off_topic" else _ESCALATE_REPLY
        return _FixedOutcome(
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            reply=reply,
            decision="escalate",
            confidence=None,
            sources=[],
            intent=intent,
            action=action,
            grounded=False,
            tokens=None,
        )

    # "question" | "other" -- the grounded path, or its ungrounded fallback
    # (SR-23) when the tenant has no embedding_model configured. RAG is
    # otherwise a hard requirement of this branch (retrieve_hybrid raises
    # RAG_EMBEDDING_NOT_CONFIGURED), which would make EVERY turn fail for a
    # tenant that has an LLM configured but hasn't set up embeddings yet --
    # never a graceful "answer without documents" path. Skipping retrieval
    # entirely here (rather than letting the exception propagate) reuses
    # _build_chitchat_prompt rather than _build_prompt(wm, []): the grounded
    # prompt's system message instructs the model to reply with the literal
    # NO_ANSWER_FOUND sentinel whenever context doesn't contain the answer,
    # and that instruction is unconditional -- it fires just as reliably on
    # an empty context block as on a real one, so re-using it here would
    # leak the raw sentinel string to the visitor on almost every real
    # question (the no-answer interception in finalize_generation only
    # runs for grounded=True). Chit-chat's prompt has no such constraint and
    # no sentinel instruction, which is exactly the shape an ungrounded,
    # general-knowledge answer needs. Mirrors chitchat's outcome shape too:
    # grounded=False, confidence=None, sources=[].
    if not config.embedding_model:
        prev_decision = await get_last_assistant_decision(db, claims, conversation_id)
        wm = await get_working_memory(
            db, claims, conversation_id, keep_recent=settings.orchestrator_history_turns,
        )
        prompt = _build_chitchat_prompt(wm)
        return _GeneratePlan(
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            prompt=prompt,
            grounded=False,
            decision="answer",
            confidence=None,
            sources=[],
            intent=intent,
            model=config.model,
            provider=provider,
            turns=turns,
            prev_decision=prev_decision,
        )

    result = await retrieve_hybrid(db, claims, message, k=settings.orchestrator_rag_k)
    confidence = result.confidence
    decision = _decide(confidence, cfg)

    if decision == "answer":
        prev_decision = await get_last_assistant_decision(db, claims, conversation_id)
        wm = await get_working_memory(
            db, claims, conversation_id, keep_recent=settings.orchestrator_history_turns,
        )
        prompt = _build_prompt(wm, result.chunks)
        sources = _sources_from_chunks(result.chunks)
        return _GeneratePlan(
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            prompt=prompt,
            grounded=True,
            decision="answer",
            confidence=confidence,
            sources=sources,
            intent=intent,
            model=config.model,
            provider=provider,
            turns=turns,
            prev_decision=prev_decision,
        )

    if decision == "clarify":
        # Repeated-low-confidence early escalate: if the preceding assistant
        # turns already ran up a streak of non-"answer" decisions (clarify
        # or escalate) reaching cfg.low_confidence_streak_cap - 1, this
        # clarify would be the Nth non-answer in a row on (very likely) the
        # same topic -- escalate now instead of asking to clarify again.
        # `None` decisions (legacy rows, or a "blocked"/"identity_gate" turn
        # in between) break the streak, same as an "answer" would -- only a
        # contiguous run of literal "clarify"/"escalate" counts.
        recent_decisions = await get_recent_assistant_decisions(
            db, claims, conversation_id, limit=cfg.low_confidence_streak_cap
        )
        streak = 0
        for prior in recent_decisions:
            if prior in ("clarify", "escalate"):
                streak += 1
            else:
                break
        if streak + 1 >= cfg.low_confidence_streak_cap:
            action = await _schedule_action(db, claims)
            await provider.aclose()
            return _FixedOutcome(
                conversation_id=conversation_id,
                assistant_id=assistant_id,
                reply=_REPEATED_LOW_CONFIDENCE_REPLY,
                decision="escalate",
                confidence=confidence,
                sources=[],
                intent=intent,
                action=action,
                grounded=False,
                tokens=None,
            )

        # Fixed-template branch -- our own trusted constant, never scanned.
        # No _GeneratePlan will carry `provider` onward, so close it here.
        await provider.aclose()
        return _FixedOutcome(
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            reply=_CLARIFY_REPLY,
            decision="clarify",
            confidence=confidence,
            sources=[],
            intent=intent,
            action=None,
            grounded=False,
            tokens=None,
        )

    # escalate (sub-floor confidence) -- fixed-template, never scanned.
    action = await _schedule_action(db, claims)
    await provider.aclose()
    return _FixedOutcome(
        conversation_id=conversation_id,
        assistant_id=assistant_id,
        reply=_ESCALATE_REPLY,
        decision="escalate",
        confidence=confidence,
        sources=[],
        intent=intent,
        action=action,
        grounded=False,
        tokens=None,
    )


def _finalize_generation(text: str, plan: _GeneratePlan) -> _FinalizedGeneration:
    """The single home of the post-generate scan/degrade logic (decision 4).

    Runs ``scan_output(text)`` on the COMPLETE generated text. On a
    violation, returns the safe reply + ``decision="blocked"`` +
    ``action="lead_form"`` + ``grounded=False`` + ``sources=[]`` +
    ``guardrail_flag=<rule>``. A clean grounded response containing the
    no-answer protocol sentinel becomes ``decision="clarify"`` only within
    the early-turn, never-twice gate; every other sentinel gets the regular
    escalation reply and the caller lazily resolves its CTA action.
    Otherwise returns the clean text + the plan's own decision/sources/
    grounded, ``action=None``, ``guardrail_flag=None``.
    Shared verbatim by ``answer_turn`` (non-streaming) and
    ``answer_turn_stream`` (streaming) -- the caller does the ``append_message``
    write and builds its own return/``done`` payload (so ``tokens`` can differ
    per path, decision 8).
    """
    guardrail = scan_output(text)
    if not guardrail.ok:
        return _FinalizedGeneration(
            reply=_GUARDRAIL_SAFE_REPLY,
            decision="blocked",
            sources=[],
            grounded=False,
            action="lead_form",
            guardrail_flag=guardrail.rule,
        )
    if plan.grounded and _NO_ANSWER_SENTINEL in text:
        if (
            plan.turns <= _NO_ANSWER_CLARIFY_MAX_TURNS
            and plan.prev_decision != "clarify"
        ):
            return _FinalizedGeneration(
                reply=_CLARIFY_REPLY,
                decision="clarify",
                sources=[],
                grounded=False,
                action=None,
                guardrail_flag=None,
            )
        return _FinalizedGeneration(
            reply=_ESCALATE_REPLY,
            decision="escalate",
            sources=[],
            grounded=False,
            action=None,
            guardrail_flag=None,
            resolve_escalate_action=True,
        )
    return _FinalizedGeneration(
        reply=text,
        decision=plan.decision,
        sources=plan.sources,
        grounded=plan.grounded,
        action=None,
        guardrail_flag=None,
    )


async def answer_turn(
    db: Database,
    claims: AuthClaims,
    *,
    message: str,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> TurnResult:
    """Run one visitor turn end-to-end (decision 8, the extended pipeline).

    Every downstream call carries ``claims`` (the caller's own VISITOR
    session) -- cross-tenant/cross-visitor context is impossible.

    Internally (S10.5 decision 1) this delegates steps 1-8 to
    ``_resolve_turn`` and the post-generate scan/degrade to
    ``_finalize_generation`` -- the externally-visible contract (signature,
    ``TurnResult``, every S10.1-S10.4 behavior) is UNCHANGED.

    Raises
    ------
    ValidationError (``LLM_NOT_CONFIGURED``)
        No LLM config for this tenant -- raised BEFORE any store write.
    ValidationError (``RAG_EMBEDDING_NOT_CONFIGURED``)
        Propagates from ``retrieve_hybrid`` -- AFTER the user turn is stored.
    NotFoundError (``CONVERSATION_NOT_FOUND``)
        A supplied ``conversation_id`` is not visible to this visitor.
    LLMError
        Propagates untouched from ``classify``, ``embed`` (via
        ``retrieve_hybrid``), or ``generate`` -- the user turn is preserved;
        the assistant turn is never written on a failed completion.
    """
    settings = get_api_settings()

    plan = await _resolve_turn(
        db, claims, message=message, conversation_id=conversation_id, message_id=message_id,
    )

    if isinstance(plan, _ReplayOutcome):
        return TurnResult(
            conversation_id=plan.conversation_id,
            message_id=plan.message_id,
            reply=plan.reply,
            decision=plan.decision,
            confidence=plan.confidence,
            sources=plan.sources,
            intent=plan.intent,
            action=plan.action,
            guardrail_flag=plan.guardrail_flag,
        )

    if isinstance(plan, _FixedOutcome):
        stored_message_id = await append_message(
            db,
            claims,
            plan.conversation_id,
            role="bot",
            content=plan.reply,
            intent=plan.intent,
            decision=plan.decision,
            grounded=plan.grounded,
            confidence=plan.confidence,
            tokens=plan.tokens,
            message_id=plan.assistant_id,
            sources=_sources_to_payload(plan.sources),
            guardrail_flag=None,
            action=plan.action,
        )
        return TurnResult(
            conversation_id=plan.conversation_id,
            message_id=stored_message_id,
            reply=plan.reply,
            decision=plan.decision,
            confidence=plan.confidence,
            sources=plan.sources,
            intent=plan.intent,
            action=plan.action,
            guardrail_flag=None,
        )

    # _GeneratePlan -- the two generate branches (grounded answer, chitchat).
    # `plan.provider` is this call's sole owner from here on -- close it
    # after generate regardless of success/failure (LLMError still needs the
    # connection pool released before propagating).
    try:
        completion = await plan.provider.generate(
            plan.prompt, model=plan.model, max_tokens=settings.llm_max_tokens,
        )
    finally:
        await plan.provider.aclose()
    final = _finalize_generation(completion.text, plan)
    action = final.action
    if final.resolve_escalate_action:
        action = await _schedule_action(db, claims)
        _log.info("post-generation no-answer escalate", extra={"event": "chat_no_answer_escalate"})
    elif final.decision == "clarify":
        _log.info("post-generation no-answer clarify", extra={"event": "chat_no_answer_clarify"})

    stored_message_id = await append_message(
        db,
        claims,
        plan.conversation_id,
        role="bot",
        content=final.reply,
        intent=plan.intent,
        decision=final.decision,
        grounded=final.grounded,
        confidence=plan.confidence,
        tokens=completion.output_tokens,
        message_id=plan.assistant_id,
        sources=_sources_to_payload(final.sources),
        guardrail_flag=final.guardrail_flag,
        action=action,
    )

    return TurnResult(
        conversation_id=plan.conversation_id,
        message_id=stored_message_id,
        reply=final.reply,
        decision=final.decision,
        confidence=plan.confidence,
        sources=final.sources,
        intent=plan.intent,
        action=action,
        guardrail_flag=final.guardrail_flag,
    )


# =====================================================================================
# S10.5: streaming delivery
# =====================================================================================


@dataclass(frozen=True)
class StreamEvent:
    """One SSE event -- ``type`` + the wire ``data`` payload.

    ``log_fields`` (when present) carries fields useful for the route's
    PII-safe log line (``intent``/``guardrail_flag``) that are deliberately
    NOT part of the wire payload (decision 5's leak-free ``done`` field set).
    ``render()`` formats the standard SSE frame; it only ever serializes
    ``data``, never ``log_fields``.
    """

    type: Literal["delta", "done", "error"]
    data: dict[str, Any]
    log_fields: dict[str, Any] | None = None

    @staticmethod
    def delta(text: str) -> StreamEvent:
        return StreamEvent(type="delta", data={"text": text})

    @staticmethod
    def done(
        *,
        conversation_id: str,
        message_id: str,
        reply: str,
        decision: str,
        confidence: float | None,
        sources: list[dict[str, Any]],
        action: str | None,
        intent: str | None = None,
        guardrail_flag: str | None = None,
    ) -> StreamEvent:
        return StreamEvent(
            type="done",
            data={
                "conversation_id": conversation_id,
                "message_id": message_id,
                "reply": reply,
                "decision": decision,
                "confidence": confidence,
                "sources": sources,
                "action": action,
            },
            log_fields={
                "decision": decision,
                "action": action,
                "intent": intent,
                "guardrail_flag": guardrail_flag,
            },
        )

    @staticmethod
    def error(code: str) -> StreamEvent:
        return StreamEvent(type="error", data={"code": code})

    def render(self) -> str:
        """Format the standard SSE frame: ``event: <type>\\ndata: <json>\\n\\n``."""
        return f"event: {self.type}\ndata: {json.dumps(self.data)}\n\n"


async def answer_turn_stream(
    db: Database,
    claims: AuthClaims,
    *,
    message: str,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Run one visitor turn, streaming the assistant reply over SSE events
    (S10.5 decisions 2/3/5).

    ``_resolve_turn`` (steps 1-8, shared with ``answer_turn``) runs FIRST --
    its exceptions (``LLM_NOT_CONFIGURED``, ``RAG_EMBEDDING_NOT_CONFIGURED``,
    ``CONVERSATION_NOT_FOUND``, a ``classify`` ``LLMError``) propagate before
    any ``StreamEvent`` is yielded, so the route can let them surface as real
    HTTP statuses before the SSE body opens (decision 7).

    - ``_ReplayOutcome`` -> one ``done``, zero ``delta``s, no store
      (decision 9).
    - ``_FixedOutcome`` -> the fixed assistant turn is stored, then one
      ``done``, zero ``delta``s (decision 2).
    - ``_GeneratePlan`` -> ``provider.stream(...)`` is iterated; each
      ``Chunk.text`` is yielded as a ``delta`` and accumulated. On a clean
      exhaustion, ``_finalize_generation`` runs on the complete accumulated
      text (decisions 3/4), the assistant turn is stored with
      ``tokens=None`` (decision 8), and one authoritative ``done`` is
      yielded -- superseding the streamed deltas on a guardrail block
      (decision 5). A mid-stream ``LLMError`` yields one ``error`` event and
      returns -- NO assistant turn is stored (decision 7/10).
    """
    plan = await _resolve_turn(
        db, claims, message=message, conversation_id=conversation_id, message_id=message_id,
    )

    if isinstance(plan, _ReplayOutcome):
        yield StreamEvent.done(
            conversation_id=plan.conversation_id,
            message_id=plan.message_id,
            reply=plan.reply,
            decision=plan.decision,
            confidence=plan.confidence,
            sources=_sources_to_payload(plan.sources),
            action=plan.action,
            intent=plan.intent,
            guardrail_flag=plan.guardrail_flag,
        )
        return

    if isinstance(plan, _FixedOutcome):
        stored_message_id = await append_message(
            db,
            claims,
            plan.conversation_id,
            role="bot",
            content=plan.reply,
            intent=plan.intent,
            decision=plan.decision,
            grounded=plan.grounded,
            confidence=plan.confidence,
            tokens=plan.tokens,
            message_id=plan.assistant_id,
            sources=_sources_to_payload(plan.sources),
            guardrail_flag=None,
            action=plan.action,
        )
        yield StreamEvent.done(
            conversation_id=plan.conversation_id,
            message_id=stored_message_id,
            reply=plan.reply,
            decision=plan.decision,
            confidence=plan.confidence,
            sources=_sources_to_payload(plan.sources),
            action=plan.action,
            intent=plan.intent,
            guardrail_flag=None,
        )
        return

    # _GeneratePlan -- stream, accumulate, finalize (decisions 2/3/4/5).
    # `plan.provider` is this call's sole owner from here on -- close it only
    # AFTER the stream is fully consumed or has failed (never mid-stream, so
    # a partial-consumer never severs the connection under a live chunk).
    settings = get_api_settings()
    parts: list[str] = []
    try:
        try:
            async for chunk in plan.provider.stream(
                plan.prompt, model=plan.model, max_tokens=settings.llm_max_tokens,
            ):
                parts.append(chunk.text)
                yield StreamEvent.delta(chunk.text)
        except LLMError:
            # Mid-stream failure: the 200 body is already open, so this cannot
            # become an HTTP status (decision 7). No assistant turn is stored --
            # only the user turn (step 5, already durable) persists (decision 10).
            yield StreamEvent.error("LLM_ERROR")
            return
    finally:
        await plan.provider.aclose()

    full_text = "".join(parts)
    final = _finalize_generation(full_text, plan)
    action = final.action
    if final.resolve_escalate_action:
        action = await _schedule_action(db, claims)
        _log.info("post-generation no-answer escalate", extra={"event": "chat_no_answer_escalate"})
    elif final.decision == "clarify":
        _log.info("post-generation no-answer clarify", extra={"event": "chat_no_answer_clarify"})

    stored_message_id = await append_message(
        db,
        claims,
        plan.conversation_id,
        role="bot",
        content=final.reply,
        intent=plan.intent,
        decision=final.decision,
        grounded=final.grounded,
        confidence=plan.confidence,
        tokens=None,  # decision 8: stream carries no usage counts.
        message_id=plan.assistant_id,
        sources=_sources_to_payload(final.sources),
        guardrail_flag=final.guardrail_flag,
        action=action,
    )

    if final.guardrail_flag is not None:
        # Decision 4: the residual gap is explicit, loudly logged, never
        # silent -- the flagged text is discarded from storage/the
        # authoritative result; only the rule (closed-set, non-PII) is
        # logged, never the flagged text itself. The rule is embedded in the
        # message text (not `extra`) because `common.logging`'s extra-field
        # allowlist (`services/common`, out of scope for S10.5) does not
        # include a "rule" key -- the message text itself has no such
        # restriction and never carries anything but the closed-set rule name.
        _log.warning(
            "chat stream guardrail block: rule=%s",
            final.guardrail_flag,
            extra={"event": "chat_stream_guardrail_block"},
        )

    yield StreamEvent.done(
        conversation_id=plan.conversation_id,
        message_id=stored_message_id,
        reply=final.reply,
        decision=final.decision,
        confidence=plan.confidence,
        sources=_sources_to_payload(final.sources),
        action=action,
        intent=plan.intent,
        guardrail_flag=final.guardrail_flag,
    )


@dataclass(frozen=True)
class PreviewResult:
    """The outcome of one stateless preview turn (Train the Agent's admin-only
    "Test the bot") -- same public shape as ``TurnResult``, minus everything
    that only makes sense for a real, persisted conversation."""

    reply: str
    decision: str
    confidence: float | None
    sources: list[Source]


async def preview_answer(db: Database, claims: AuthClaims, message: str) -> PreviewResult:
    """Stateless, single-turn preview of the real RAG/orchestrator pipeline
    (Train the Agent's admin-only "Test the bot"). Reuses the SAME tenant
    config resolution, intent classification, RAG retrieval, prompt-building,
    confidence-band decision, and post-generate guardrail scan as a real
    visitor turn (``_resolve_turn``'s steps 1-2/7/8 + ``_finalize_generation``)
    -- but writes NOTHING to ``conversation_store``: no conversation, no
    message rows, ever (the test suite spies on ``append_message``/
    ``create_conversation`` and asserts zero calls).

    Deliberately simpler than a real turn in one respect: no conversation
    history. Each call is independent (empty working memory), so there is no
    turn-cap, no low-confidence-streak early escalate, and no identity gate --
    all three depend on conversation state a stateless preview has none of.
    This answers "what would the bot say to this ONE question right now",
    which is what an admin verifying a taught answer needs -- it is not a
    multi-turn conversation simulator.

    Raises ``ValidationError`` (``LLM_NOT_CONFIGURED``) exactly like
    ``answer_turn``. ``RAG_EMBEDDING_NOT_CONFIGURED``/``LLMError`` propagate
    untouched, same as a real turn's runtime failures.
    """
    config = await get_llm_config(db, claims)
    if config is None:
        raise ValidationError(
            "LLM is not configured for this tenant.",
            code="LLM_NOT_CONFIGURED",
        )
    cfg = await get_orchestrator_config(db, claims)
    settings = get_api_settings()
    empty_wm: dict[str, Any] = {"summary": None, "summary_message_count": 0, "messages": []}

    provider = provider_for(config)
    try:
        intent = await provider.classify(
            message,
            _INTENT_LABELS,
            model=config.model,
            label_descriptions=_INTENT_LABEL_DESCRIPTIONS,
        )

        if intent in ("scheduling_request", "off_topic"):
            reply = _OFF_TOPIC_REPLY if intent == "off_topic" else _ESCALATE_REPLY
            return PreviewResult(reply=reply, decision="escalate", confidence=None, sources=[])

        if intent == "chitchat" or not config.embedding_model:
            plan = _GeneratePlan(
                conversation_id="",
                assistant_id=None,
                prompt=_build_chitchat_prompt(empty_wm),
                grounded=False,
                decision="answer",
                confidence=None,
                sources=[],
                intent=intent,
                model=config.model,
                provider=provider,
                turns=1,
                prev_decision=None,
            )
        else:
            result = await retrieve_hybrid(db, claims, message, k=settings.orchestrator_rag_k)
            decision = _decide(result.confidence, cfg)
            if decision != "answer":
                reply = _CLARIFY_REPLY if decision == "clarify" else _ESCALATE_REPLY
                return PreviewResult(
                    reply=reply, decision=decision, confidence=result.confidence, sources=[],
                )
            plan = _GeneratePlan(
                conversation_id="",
                assistant_id=None,
                prompt=_build_prompt(empty_wm, result.chunks),
                grounded=True,
                decision="answer",
                confidence=result.confidence,
                sources=_sources_from_chunks(result.chunks),
                intent=intent,
                model=config.model,
                provider=provider,
                turns=1,
                prev_decision=None,
            )

        completion = await provider.generate(
            plan.prompt, model=plan.model, max_tokens=settings.llm_max_tokens,
        )
        finalized = _finalize_generation(completion.text, plan)
        # Mirrors answer_turn's own TurnResult construction: confidence is
        # always the ORIGINALLY computed value, even when finalize flips the
        # decision (guardrail block / no-answer sentinel) -- never blanked.
        return PreviewResult(
            reply=finalized.reply,
            decision=finalized.decision,
            confidence=plan.confidence,
            sources=finalized.sources,
        )
    finally:
        await provider.aclose()
