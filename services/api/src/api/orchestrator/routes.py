"""Chat orchestrator routes -- POST /public/chat/message (S10.1).

Mirrors the ``/public/leads`` shape: visitor-authenticated
(``get_visitor_claims``), leak-free response (no ``tenant_id``/``visitor_id``
ever), and ``tenant_id``/``visitor_id`` come only from the signed visitor
session -- never the request body.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal

from common.auth import AuthClaims
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from api.gateway.dependencies import get_visitor_claims
from api.orchestrator.service import StreamEvent, answer_turn, answer_turn_stream

_log = get_logger(__name__)

router = APIRouter(prefix="/public/chat", tags=["chat"])


class ChatMessageRequest(BaseModel):
    """Body for POST /public/chat/message."""

    message: str
    conversation_id: str | None = None
    message_id: str | None = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must not be blank")
        return v


class ChatSource(BaseModel):
    """A cited chunk in the response -- identifiers + score only, never raw content."""

    doc_id: str
    chunk_id: str
    score: float | None
    matched_by: list[str]


class ChatMessageResponse(BaseModel):
    """Leak-free response body for POST /public/chat/message.

    ``intent``/``grounded``/``guardrail_flag`` are stored (analytics) but
    never surfaced here -- keep the public surface minimal; still leak-free
    (never ``tenant_id``/``visitor_id``). ``action`` (S10.3, widened S10.4,
    widened again SR-14) IS surfaced -- it tells the widget whether to render
    the S8.1 scheduling CTA (``"schedule_cta"``), the S7.1 consent-gated lead
    form (``"lead_form"``) on a ``decision`` that means "offer a human"
    (``escalate``/``blocked``), or the SR-14 consent-gated identity form
    (``"identity_form"``) on ``decision="identity_gate"`` -- a distinct
    decision value (not a reuse of ``clarify``/``escalate``) so gate-shown
    counts are a stored, tagged fact (SR-14 D8/D11).
    """

    conversation_id: str
    message_id: str
    reply: str
    decision: Literal["answer", "clarify", "escalate", "blocked", "identity_gate"]
    confidence: float | None
    sources: list[ChatSource]
    action: Literal["lead_form", "schedule_cta", "identity_form"] | None = None


@router.post("/message")
async def post_message(
    body: ChatMessageRequest,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> ChatMessageResponse:
    """Run one visitor turn: store -> classify -> branch -> store.

    ``tenant_id`` and ``visitor_id`` come from the visitor session
    (``claims``), never from the request body. Errors from ``answer_turn``
    (``LLM_NOT_CONFIGURED``/``RAG_EMBEDDING_NOT_CONFIGURED`` 422,
    ``LLM_ERROR`` 502 -- including a ``classify`` failure, S10.2 decision 9,
    ``CONVERSATION_NOT_FOUND`` 404) propagate to the centralized error
    middleware.
    """
    db = request.app.state.db

    result = await answer_turn(
        db,
        claims,
        message=body.message,
        conversation_id=body.conversation_id,
        message_id=body.message_id,
    )

    # -- Log the event (PII-safe: never the message text, reply, or raw
    # intent free text -- decision/intent/guardrail_flag/action are
    # closed-set, non-PII labels) ---------------------------------------------
    _log.info(
        "chat turn",
        extra={
            "event": "chat_turn",
            "tenant_id": claims.tenant_id,
            "conversation_id": result.conversation_id,
            "confidence": result.confidence,
            "decision": result.decision,
            "intent": result.intent,
            "guardrail_flag": result.guardrail_flag,
            "action": result.action,
        },
    )

    return ChatMessageResponse(
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        reply=result.reply,
        decision=result.decision,  # type: ignore[arg-type]
        confidence=result.confidence,
        sources=[
            ChatSource(doc_id=s.doc_id, chunk_id=s.chunk_id, score=s.score, matched_by=s.matched_by)
            for s in result.sources
        ],
        action=result.action,  # type: ignore[arg-type]
    )


@router.post("/message/stream")
async def post_message_stream(
    body: ChatMessageRequest,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> StreamingResponse:
    """Stream one visitor turn over Server-Sent Events (S10.5).

    Same visitor auth, same ``ChatMessageRequest`` body, same tenant/visitor
    scoping as ``POST /public/chat/message`` -- this is a NEW, separate
    endpoint (decision 6); the existing JSON endpoint above is untouched.

    Three named SSE events (decision 5): zero or more ``delta``
    (``{"text": "<chunk>"}``, only while a grounded-answer/chit-chat
    generation streams), exactly one terminal ``done`` (the same leak-free
    field set as ``ChatMessageResponse`` -- never ``tenant_id``/
    ``visitor_id``/``intent``/``grounded``/``guardrail_flag``) on every
    successful turn, or one ``error`` (``{"code": "LLM_ERROR"}``) on a
    mid-stream provider failure (no ``done`` follows an ``error``).

    **Client contract:** render ``delta``s progressively, but treat
    ``done.reply``/``done.decision``/``done.action`` as authoritative and
    reconcile (replace) the displayed text with ``done.reply`` when it
    arrives -- on a guardrail block, ``done.reply`` is the safe reply and
    *supersedes* any deltas that already streamed (decision 4/5).

    **Failure taxonomy (decision 7 -- CRITICAL):** ``answer_turn_stream`` is
    an async generator whose very first statement (before any ``yield``) is
    ``await _resolve_turn(...)`` -- the same pre-generation pipeline
    ``answer_turn`` runs (config fail-fast, get-or-create, idempotent
    replay, user-turn store, turn-cap, classify, RAG/decide/schedule-action).
    Calling the generator function does NOT execute any of that -- Python
    only runs an async generator's body up to its first ``yield`` (or a
    raised exception) once the FIRST item is pulled. So this handler
    deliberately "primes" the generator with one ``__anext__()`` call
    BEFORE constructing ``StreamingResponse``: any exception from
    ``_resolve_turn`` (``LLM_NOT_CONFIGURED`` 422, ``RAG_EMBEDDING_NOT_CONFIGURED``
    422, a ``classify`` ``LLMError`` -> 502, ``CONVERSATION_NOT_FOUND`` 404)
    propagates right here, before HTTP 200 + headers are committed, and
    surfaces as a normal JSON error response via the centralized error
    middleware -- never as an ``error`` SSE frame. Only a `provider.stream`
    failure that occurs AFTER this priming step (i.e. after the first delta,
    mid-generation) becomes an ``error`` event, because by then the 200 body
    is already open and the status code can no longer change.
    """
    db = request.app.state.db

    events = answer_turn_stream(
        db,
        claims,
        message=body.message,
        conversation_id=body.conversation_id,
        message_id=body.message_id,
    )

    # Prime the generator: this executes _resolve_turn (and everything up to
    # the first `yield`) NOW, synchronously within the route coroutine --
    # before StreamingResponse is ever constructed. Any pre-generation
    # exception raised inside _resolve_turn propagates from this `await` as
    # a normal exception, handled by the centralized error middleware with
    # the correct HTTP status -- decision 7's hard requirement.
    try:
        first_event: StreamEvent | None = await events.__anext__()
    except StopAsyncIteration:
        first_event = None

    async def _frames() -> AsyncIterator[str]:
        last_event = first_event
        if first_event is not None:
            yield first_event.render()
        async for ev in events:
            last_event = ev
            yield ev.render()

        # PII-safe exit log (never the message/reply/flagged text -- only
        # closed-set, non-PII fields: decision/action/intent/guardrail_flag).
        if last_event is not None and last_event.type == "done":
            fields = last_event.log_fields or {}
            _log.info(
                "chat stream turn",
                extra={
                    "event": "chat_stream_turn",
                    "tenant_id": claims.tenant_id,
                    "conversation_id": last_event.data.get("conversation_id"),
                    "decision": fields.get("decision"),
                    "intent": fields.get("intent"),
                    "action": fields.get("action"),
                    "guardrail_flag": fields.get("guardrail_flag"),
                },
            )
        elif last_event is not None and last_event.type == "error":
            _log.info(
                "chat stream turn",
                extra={
                    "event": "chat_stream_turn",
                    "tenant_id": claims.tenant_id,
                    "decision": None,
                    "intent": None,
                    "action": None,
                    "guardrail_flag": None,
                },
            )

    return StreamingResponse(_frames(), media_type="text/event-stream")
