"""Admin/agent conversation review routes -- GET /admin/conversations(/{id}).

An authenticated ``CLIENT_ADMIN`` or ``CLIENT_AGENT`` lists their tenant's
conversations (paginated, filterable) and opens the full transcript of one.
This is the read-only "conversation review" console surface (S12.4) -- a new
sibling of the existing ``/debug/conversations/**`` routes (``CLIENT_ADMIN``
-only, id-required, not an enumeration surface) and distinct from S11.2's
aggregate ``/admin/analytics/overview``. Every access is tenant-scoped via
``claims.tenant_id`` -- a cross-tenant ``conversation_id`` is indistinguishable
from a missing one and returns 404. Responses never include ``tenant_id``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from api.auth.dependencies import require_roles, resolve_tenant_scope
from api.conversation_store.repository import (
    ConversationSummaryRow,
    get_conversation,
    get_message,
    get_messages,
    list_conversations,
)
from api.rag.repository import resolve_chunks

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/conversations", tags=["conversations"])
tenant_scoped_router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/conversations", tags=["conversations"]
)

_VALID_STATUSES: set[str] = {"active", "ended"}


class ConversationListItem(BaseModel):
    """A single row in the paginated ``GET /admin/conversations`` list --
    leak-free (no ``tenant_id``)."""

    conversation_id: str
    status: str
    channel: str
    visitor_id: str | None
    started_at: datetime
    ended_at: datetime | None
    message_count: int
    summary: str | None


class ConversationListResponse(BaseModel):
    """Paginated envelope for ``GET /admin/conversations`` -- mirrors
    ``LeadListResponse``'s extension of the ``api/audit/routes.py``
    limit/offset convention with ``total``."""

    items: list[ConversationListItem]
    total: int
    limit: int
    offset: int


class MessageResponse(BaseModel):
    """A single transcript message -- leak-free (no ``tenant_id``)."""

    message_id: str
    role: str
    content: str
    intent: str | None
    confidence: float | None
    tokens: int | None
    created_at: datetime
    source_count: int
    """Cheap hint (``jsonb_array_length(sources)``, SR-2) so the transcript
    UI knows which bot messages have a "View sources" affordance -- NOT the
    full sources payload (that's the separate ``/messages/{id}/sources``
    route, fetched lazily on expand)."""


class ConversationDetailResponse(BaseModel):
    """Full transcript detail for ``GET /admin/conversations/{conversation_id}``
    -- leak-free (no ``tenant_id``)."""

    conversation_id: str
    status: str
    channel: str
    started_at: datetime
    ended_at: datetime | None
    summary: str | None
    messages: list[MessageResponse]


def _to_list_item(row: ConversationSummaryRow) -> ConversationListItem:
    return ConversationListItem(
        conversation_id=row.conversation_id,
        status=row.status,
        channel=row.channel,
        visitor_id=row.visitor_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        message_count=row.message_count,
        summary=row.summary,
    )


async def _list_conversations(
    request: Request,
    claims: AuthClaims,
    *,
    limit: int,
    offset: int,
    status_: str | None,
    channel: str | None,
    escalated: bool | None,
    started_from: datetime | None,
    started_to: datetime | None,
) -> ConversationListResponse:
    """List/filter the caller's tenant conversations, newest first, paginated.

    ``status`` is validated against ``{active, ended}`` (422
    ``INVALID_CONVERSATION_FILTER`` on an unknown value). ``channel`` is an
    exact-match pass-through. ``escalated`` restricts to conversations with
    (``true``) / without (``false``) a bot turn with ``decision='escalate'``.
    ``from``/``to`` filter ``started_at`` as a half-open ``[from, to)`` window
    (422 ``INVALID_LIST_WINDOW`` if ``from >= to``). ``limit`` clamped to
    ``[1, 200]``, ``offset`` to ``>= 0``.
    """
    db = request.app.state.db

    if status_ is not None and status_ not in _VALID_STATUSES:
        raise ValidationError(
            f"Unknown status filter {status_!r}.", code="INVALID_CONVERSATION_FILTER",
        )
    if started_from is not None and started_to is not None and started_from >= started_to:
        raise ValidationError(
            "`from` must be earlier than `to`.", code="INVALID_LIST_WINDOW",
        )

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)

    rows, total = await list_conversations(
        db,
        claims,
        limit=clamped_limit,
        offset=clamped_offset,
        started_from=started_from,
        started_to=started_to,
        status=status_,
        channel=channel,
        escalated=escalated,
    )

    filter_keys: dict[str, Any] = {
        "status": status_,
        "channel": channel,
        "escalated": escalated,
        "from": started_from,
        "to": started_to,
    }
    _log.info(
        "conversations listed",
        extra={
            "event": "conversations_listed",
            "tenant_id": claims.tenant_id,
            "filter_keys": sorted(k for k, v in filter_keys.items() if v is not None),
            "result_count": len(rows),
        },
    )

    return ConversationListResponse(
        items=[_to_list_item(r) for r in rows],
        total=total,
        limit=clamped_limit,
        offset=clamped_offset,
    )


@router.get("")
async def list_conversations_route(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    status_: str | None = Query(default=None, alias="status"),
    channel: str | None = Query(default=None),
    escalated: bool | None = Query(default=None),
    started_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    started_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ConversationListResponse:
    return await _list_conversations(
        request, claims,
        limit=limit, offset=offset, status_=status_, channel=channel,
        escalated=escalated, started_from=started_from, started_to=started_to,
    )


@tenant_scoped_router.get("")
async def list_conversations_route_for_tenant(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    status_: str | None = Query(default=None, alias="status"),
    channel: str | None = Query(default=None),
    escalated: bool | None = Query(default=None),
    started_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    started_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ConversationListResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/conversations`` (S12.7)."""
    return await _list_conversations(
        request, claims,
        limit=limit, offset=offset, status_=status_, channel=channel,
        escalated=escalated, started_from=started_from, started_to=started_to,
    )


async def _get_conversation_detail(
    conversation_id: str,
    request: Request,
    claims: AuthClaims,
) -> ConversationDetailResponse:
    """Fetch a conversation's full transcript. Reuses ``get_conversation`` +
    ``get_messages`` (already tenant-scoped) -- no new single-conversation
    read logic. Returns 404 ``CONVERSATION_NOT_FOUND`` if missing or
    cross-tenant.
    """
    db = request.app.state.db

    conv = await get_conversation(db, claims, conversation_id)
    if conv is None:
        raise NotFoundError(
            "Conversation not found.", code="CONVERSATION_NOT_FOUND",
        )

    messages = await get_messages(db, claims, conversation_id)

    _log.info(
        "conversation viewed",
        extra={
            "event": "conversation_viewed",
            "conversation_id": conversation_id,
            "tenant_id": claims.tenant_id,
            "message_count": len(messages),
        },
    )

    return ConversationDetailResponse(
        conversation_id=conv.conversation_id,
        status=conv.status,
        channel=conv.channel,
        started_at=conv.started_at,
        ended_at=conv.ended_at,
        summary=conv.summary,
        messages=[
            MessageResponse(
                message_id=m.message_id,
                role=m.role,
                content=m.content,
                intent=m.intent,
                confidence=m.confidence,
                tokens=m.tokens,
                created_at=m.created_at,
                source_count=m.source_count,
            )
            for m in messages
        ],
    )


@router.get("/{conversation_id}")
async def get_conversation_detail(
    conversation_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ConversationDetailResponse:
    return await _get_conversation_detail(conversation_id, request, claims)


@tenant_scoped_router.get("/{conversation_id}")
async def get_conversation_detail_for_tenant(
    conversation_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ConversationDetailResponse:
    """PLATFORM_ADMIN super-user variant of
    ``GET /admin/conversations/{conversation_id}`` (S12.7)."""
    return await _get_conversation_detail(conversation_id, request, claims)


# ---------------------------------------------------------------------------
# Grounding spot-check: GET .../{conversation_id}/messages/{message_id}/sources
# (SR-2) -- resolves a bot message's stored `sources` (doc_id/chunk_id/score/
# matched_by) to the real, live `knowledge_chunks.content` so a reviewer can
# read the reply next to what it was supposedly grounded in and judge
# groundedness for themselves. Read-only; no scoring/diff/verdict (decision 6).
# ---------------------------------------------------------------------------


class MessageSourceItem(BaseModel):
    """A single resolved citation -- leak-free (no ``tenant_id``).

    ``chunk_id``/``doc_id``/``score``/``matched_by`` come from the message's
    stored (historical) ``sources`` entry; ``content``/``resolved`` come from
    the LIVE ``knowledge_chunks`` lookup. ``content`` is ``None`` and
    ``resolved`` is ``False`` when the chunk no longer resolves (deleted/
    re-ingested, or -- by construction -- a cross-tenant id) -- never
    silently dropped, never given placeholder text (no silent fallback).
    """

    chunk_id: str
    doc_id: str
    score: float | None
    matched_by: list[str]
    content: str | None
    resolved: bool


class MessageSourcesResponse(BaseModel):
    """Response for the grounding spot-check endpoint -- leak-free (no
    ``tenant_id``). ``sources`` is always present (an empty list, never a
    404/422, for a message with no citations -- decision 7)."""

    message_id: str
    content: str
    decision: str | None
    confidence: float | None
    grounded: bool | None
    sources: list[MessageSourceItem]


async def _get_message_sources(
    conversation_id: str,
    message_id: str,
    request: Request,
    claims: AuthClaims,
) -> MessageSourcesResponse:
    """Fetch a message (tenant-scoped via ``get_message``) and resolve its
    stored ``sources`` chunk_ids to live content (tenant-scoped via
    ``resolve_chunks`` -- the tenant filter is INSIDE that query, so a
    cross-tenant/guessed chunk_id can never resolve). Returns 404
    ``MESSAGE_NOT_FOUND`` if the message is absent or not visible to the
    caller (indistinguishable from cross-tenant, no leak). A message with no
    stored sources (``NULL`` or ``[]``) returns ``sources: []`` with a 200 --
    never an error (decision 7).
    """
    db = request.app.state.db

    msg = await get_message(db, claims, conversation_id, message_id)
    if msg is None:
        raise NotFoundError(
            "Message not found.", code="MESSAGE_NOT_FOUND",
        )

    stored_sources = msg.sources or []
    chunk_ids = [str(s["chunk_id"]) for s in stored_sources]
    resolved = await resolve_chunks(db, claims, chunk_ids)

    items = [
        MessageSourceItem(
            chunk_id=str(s["chunk_id"]),
            doc_id=str(s.get("doc_id", "")),
            score=s.get("score"),
            matched_by=list(s.get("matched_by") or []),
            content=resolved.get(str(s["chunk_id"])),
            resolved=str(s["chunk_id"]) in resolved,
        )
        for s in stored_sources
    ]

    resolved_count = sum(1 for item in items if item.resolved)
    unresolved_count = len(items) - resolved_count
    _log.info(
        "message_sources_viewed conversation_id=%s message_id=%s "
        "source_count=%d resolved_count=%d unresolved_count=%d",
        conversation_id,
        message_id,
        len(items),
        resolved_count,
        unresolved_count,
        extra={
            "event": "message_sources_viewed",
            "tenant_id": claims.tenant_id,
            "conversation_id": conversation_id,
        },
    )

    return MessageSourcesResponse(
        message_id=msg.message_id,
        content=msg.content,
        decision=msg.decision,
        confidence=msg.confidence,
        grounded=msg.grounded,
        sources=items,
    )


@router.get("/{conversation_id}/messages/{message_id}/sources")
async def get_message_sources(
    conversation_id: str,
    message_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> MessageSourcesResponse:
    return await _get_message_sources(conversation_id, message_id, request, claims)


@tenant_scoped_router.get("/{conversation_id}/messages/{message_id}/sources")
async def get_message_sources_for_tenant(
    conversation_id: str,
    message_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> MessageSourcesResponse:
    """PLATFORM_ADMIN super-user variant of the grounding spot-check
    endpoint (S12.7 pattern)."""
    return await _get_message_sources(conversation_id, message_id, request, claims)
