"""Train the Agent endpoints -- test-bot chat, coverage gaps, teach an answer.

All three require ``CLIENT_ADMIN`` (RBAC, CLAUDE.md §3) -- matches
``ingestion/routes.py``'s convention (knowledge-content authority), not
``leads``' agent-inclusive one, since teaching an answer writes directly into
the tenant's knowledge base.

POST /admin/training/chat
    Stateless "test the bot" -- runs ``orchestrator.service.preview_answer``
    (the real RAG/orchestrator pipeline, single independent turn, NO
    conversation_store writes) and returns ``{reply, decision, confidence,
    sources}``. Not a mutation, so no audit event.

GET /admin/training/gaps
    "Coverage check" -- recent real visitor turns where the bot didn't
    answer (``decision in (escalate, clarify)``), paired with the question
    that triggered them, excluding anything already taught (exact-match on
    normalized question text). Returns ``{"gaps": [...]}``.

POST /admin/training/suggest-answer
    "Suggest a reply" (Teach the correct answer) -- a best-effort DRAFT
    answer for the admin to review/edit before saving, offered only after the
    bot has already failed to answer this exact question. Runs
    ``orchestrator.service.suggest_draft_answer`` (same LLM/RAG stack as
    ``preview_answer``, but bypasses the confidence gate and guardrail pass
    entirely -- it always drafts something). Returns ``{suggestion}``. Not a
    mutation (nothing is saved until the admin explicitly hits "Save
    answer"), so no audit event.

POST /admin/training/answer
    "Teach the correct answer" -- pushes ``Q: {question}\\n\\nA: {answer}``
    through the EXACT SAME ingestion path a real file upload uses (hash ->
    store -> ``create_doc`` -> ``create_run`` -> enqueue
    ``ingestion.ingest_document``), then records a ``training_answers`` row
    so this question stops showing up as a gap. Returns immediately
    (``{doc_id, run_id, training_answer_id}``) -- does not wait for the
    async ingestion run to finish, matching the existing upload endpoint's
    behavior.

POST /admin/training/dismiss
    "Not a real gap" -- records a ``training_answers`` row with
    ``dismissed=true`` and no answer/doc, so a junk/adversarial visitor
    message (e.g. "I won't.") stops showing up in the coverage feed WITHOUT
    inventing a fake taught answer or touching the knowledge base at all.
    Returns ``{training_answer_id}``.
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal
from uuid import uuid4

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, field_validator

from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.conversation_store.repository import list_low_confidence_messages
from api.ingestion import repository as ingestion_repo
from api.ingestion.storage import get_storage
from api.ingestion.tasks import ingest_document
from api.orchestrator.service import preview_answer, suggest_draft_answer
from api.training.repository import (
    create_training_answer,
    list_taught_question_keys,
    normalize_question,
)

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/training", tags=["training"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/training", tags=["training"])


class ChatRequest(BaseModel):
    """Body for POST /admin/training/chat."""

    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must not be blank")
        return v


class ChatSource(BaseModel):
    doc_id: str
    chunk_id: str
    score: float | None
    matched_by: list[str]


class ChatResponse(BaseModel):
    reply: str
    decision: Literal["answer", "clarify", "escalate", "blocked"]
    confidence: float | None
    sources: list[ChatSource]


class SuggestAnswerRequest(BaseModel):
    """Body for POST /admin/training/suggest-answer."""

    question: str

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("question must not be blank")
        return v


class SuggestAnswerResponse(BaseModel):
    suggestion: str


class AnswerRequest(BaseModel):
    """Body for POST /admin/training/answer."""

    question: str
    answer: str
    source_message_id: str | None = None

    @field_validator("question", "answer")
    @classmethod
    def validate_non_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v


class DismissRequest(BaseModel):
    """Body for POST /admin/training/dismiss."""

    question: str
    source_message_id: str | None = None

    @field_validator("question")
    @classmethod
    def validate_non_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v


async def _chat_preview(request: Request, claims: AuthClaims, body: ChatRequest) -> ChatResponse:
    db = request.app.state.db
    result = await preview_answer(db, claims, body.message)
    return ChatResponse(
        reply=result.reply,
        decision=result.decision,  # type: ignore[arg-type]
        confidence=result.confidence,
        sources=[
            ChatSource(doc_id=s.doc_id, chunk_id=s.chunk_id, score=s.score, matched_by=s.matched_by)
            for s in result.sources
        ],
    )


@router.post("/chat")
async def post_chat(
    body: ChatRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ChatResponse:
    return await _chat_preview(request, claims, body)


@tenant_scoped_router.post("/chat")
async def post_chat_for_tenant(
    body: ChatRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ChatResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/training/chat``."""
    return await _chat_preview(request, claims, body)


async def _suggest_answer(
    request: Request, claims: AuthClaims, body: SuggestAnswerRequest,
) -> SuggestAnswerResponse:
    db = request.app.state.db
    suggestion = await suggest_draft_answer(db, claims, body.question)
    return SuggestAnswerResponse(suggestion=suggestion)


@router.post("/suggest-answer")
async def post_suggest_answer(
    body: SuggestAnswerRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> SuggestAnswerResponse:
    return await _suggest_answer(request, claims, body)


@tenant_scoped_router.post("/suggest-answer")
async def post_suggest_answer_for_tenant(
    body: SuggestAnswerRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> SuggestAnswerResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/training/suggest-answer``."""
    return await _suggest_answer(request, claims, body)


async def _list_gaps(request: Request, claims: AuthClaims, *, limit: int) -> dict[str, Any]:
    db = request.app.state.db
    gaps = await list_low_confidence_messages(db, claims, limit=limit)
    taught = await list_taught_question_keys(db, claims)
    filtered = [g for g in gaps if normalize_question(g.question) not in taught]
    return {
        "gaps": [
            {
                "message_id": g.message_id,
                "question": g.question,
                "question_message_id": g.question_message_id,
                "decision": g.decision,
                "confidence": g.confidence,
                "created_at": g.created_at,
            }
            for g in filtered
        ]
    }


@router.get("/gaps")
async def get_gaps(
    request: Request,
    limit: int = 20,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    return await _list_gaps(request, claims, limit=limit)


@tenant_scoped_router.get("/gaps")
async def get_gaps_for_tenant(
    request: Request,
    limit: int = 20,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/training/gaps``."""
    return await _list_gaps(request, claims, limit=limit)


async def _teach_answer(
    request: Request, claims: AuthClaims, body: AnswerRequest,
) -> dict[str, Any]:
    from common.logging import _correlation_id  # noqa: PLC2701, PLC0415

    cid = _correlation_id.get() or ""
    db = request.app.state.db

    # Same synthetic-doc shape as the ingestion pipeline expects -- reuses
    # its parse/chunk/embed pipeline verbatim, zero new ingestion code.
    content = f"Q: {body.question}\n\nA: {body.answer}".encode()
    content_hash = hashlib.sha256(content).hexdigest()

    existing = await ingestion_repo.find_doc_by_hash(db, claims, content_hash)
    if existing is not None:
        doc_id = existing.doc_id
        run_id = None
    else:
        doc_id = uuid4().hex
        filename = f"trained-{doc_id}.txt"
        storage_key = f"{claims.tenant_id}/{doc_id}/{filename}"
        get_storage().put(storage_key, content)

        await ingestion_repo.create_doc(
            db,
            claims,
            source="training",
            filename=filename,
            content_type="text/plain",
            content_hash=content_hash,
            storage_key=storage_key,
            doc_id=doc_id,
            title=body.question[:120],
            description="Trained answer",
            uploaded_by=claims.subject,
        )
        run = await ingestion_repo.create_run(db, claims, doc_id=doc_id)
        run_id = run.run_id
        ingest_document.delay(
            doc_id=doc_id, tenant_id=claims.tenant_id, run_id=run_id, correlation_id=cid,
        )

    training_answer = await create_training_answer(
        db,
        claims,
        question=body.question,
        answer=body.answer,
        doc_id=doc_id,
        source_message_id=body.source_message_id,
    )

    await record_audit(
        db,
        claims,
        action="training_answer_created",
        target_type="knowledge_doc",
        target_id=doc_id,
        metadata={"question": body.question[:200]},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info("training_answer_created", extra={"event": "training_answer_created"})

    return {"doc_id": doc_id, "run_id": run_id, "training_answer_id": training_answer.id}


@router.post("/answer")
async def post_answer(
    body: AnswerRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    return await _teach_answer(request, claims, body)


@tenant_scoped_router.post("/answer")
async def post_answer_for_tenant(
    body: AnswerRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/training/answer``."""
    return await _teach_answer(request, claims, body)


async def _dismiss_gap(
    request: Request, claims: AuthClaims, body: DismissRequest,
) -> dict[str, Any]:
    db = request.app.state.db

    training_answer = await create_training_answer(
        db,
        claims,
        question=body.question,
        answer=None,
        doc_id=None,
        source_message_id=body.source_message_id,
        dismissed=True,
    )

    await record_audit(
        db,
        claims,
        action="training_gap_dismissed",
        target_type="training_answer",
        target_id=training_answer.id,
        metadata={"question": body.question[:200]},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info("training_gap_dismissed", extra={"event": "training_gap_dismissed"})

    return {"training_answer_id": training_answer.id}


@router.post("/dismiss")
async def post_dismiss(
    body: DismissRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    return await _dismiss_gap(request, claims, body)


@tenant_scoped_router.post("/dismiss")
async def post_dismiss_for_tenant(
    body: DismissRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/training/dismiss``."""
    return await _dismiss_gap(request, claims, body)
