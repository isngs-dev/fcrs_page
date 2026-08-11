"""Admin/agent lead pipeline routes -- GET and PATCH /admin/leads/{lead_id}.

An authenticated ``CLIENT_ADMIN`` or ``CLIENT_AGENT`` reviews a lead and
moves it through the pipeline state machine (``api.leads.pipeline``). Every
access is tenant-scoped via ``claims.tenant_id`` -- a cross-tenant
``lead_id`` is indistinguishable from a missing one and returns 404.

The response is an authenticated admin/agent surface, so it MAY include
contact fields (unlike the leak-free ``/public/leads`` response) -- but
``tenant_id`` and raw consent text are never included, and the transition
log never carries PII.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.accounts.repository import get_account
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.auth.repository import get_user_by_id
from api.contacts.repository import add_identity as add_contact_identity
from api.contacts.repository import create_contact
from api.leads.pipeline import (
    _STATUS_BY_STAGE,
    STAGE_ORDER,
    TERMINAL_STAGES,
    compute_qualification_score,
    status_for_stage,
    validate_transition,
)
from api.leads.repository import (
    LEAD_SORT_DEFAULT_DIRECTIONS,
    LEAD_SORT_KEYS,
    Lead,
    LeadActivity,
    add_activity,
    assign_lead,
    get_lead,
    list_activities,
    list_leads,
    list_leads_for_export,
    mark_lead_converted,
    update_lead_stage,
)
from api.notifications.emit import emit_event_safe

_log = get_logger(__name__)

_NOTE_MAX_LENGTH = 4000

router = APIRouter(prefix="/admin/leads", tags=["leads"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/leads", tags=["leads"])


class LeadStageUpdateRequest(BaseModel):
    """Body for PATCH /admin/leads/{lead_id}."""

    stage: str


class LeadNoteRequest(BaseModel):
    """Body for POST /admin/leads/{lead_id}/notes."""

    text: str = Field(min_length=1, max_length=_NOTE_MAX_LENGTH)

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Note text must not be blank.")
        return value


class LeadAssignmentRequest(BaseModel):
    """Body for POST /admin/leads/{lead_id}/assignment."""

    agent_id: str


class LeadActivityResponse(BaseModel):
    """Leak-free (no ``tenant_id``) activity for the admin/agent timeline surface."""

    activity_id: str
    lead_id: str
    type: str
    payload: dict[str, Any] | None
    actor: str | None
    created_at: datetime


def _to_activity_response(activity: LeadActivity) -> LeadActivityResponse:
    return LeadActivityResponse(
        activity_id=activity.activity_id,
        lead_id=activity.lead_id,
        type=activity.type,
        payload=activity.payload,
        actor=activity.actor,
        created_at=activity.created_at,
    )


class LeadDetailResponse(BaseModel):
    """Leak-free (no ``tenant_id``) lead detail for the admin/agent surface.

    ``name``/``email`` are nullable (SR-9.1 C4) -- an anonymous
    booking-created lead (``source="booking"``) may have NULL contact info;
    the admin surface reflects that honestly rather than fabricating a value.
    """

    lead_id: str
    name: str | None
    email: str | None
    phone: str | None
    status: str
    stage: str
    qualification_score: int | None
    assigned_agent_id: str | None
    source: str
    converted_to_contact_id: str | None = None


def _to_response(lead: Lead) -> LeadDetailResponse:
    return LeadDetailResponse(
        lead_id=lead.lead_id,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        status=lead.status,
        stage=lead.stage,
        qualification_score=lead.qualification_score,
        assigned_agent_id=lead.assigned_agent_id,
        source=lead.source,
        converted_to_contact_id=lead.converted_to_contact_id,
    )


_VALID_STAGES: set[str] = set(STAGE_ORDER) | TERMINAL_STAGES
_VALID_STATUSES: set[str] = set(_STATUS_BY_STAGE.values())


class LeadListItem(LeadDetailResponse):
    """A single row in the paginated ``GET /admin/leads`` list -- the same
    leak-free ``LeadDetailResponse`` fields plus ``created_at``."""

    created_at: datetime


class LeadListResponse(BaseModel):
    """Paginated envelope for ``GET /admin/leads`` -- reuses the
    ``api/audit/routes.py`` limit/offset convention, extended with ``total``
    (a review console needs the total to page, unlike audit's scrolling tail).
    """

    items: list[LeadListItem]
    total: int
    limit: int
    offset: int


def _to_list_item(lead: Lead) -> LeadListItem:
    return LeadListItem(
        lead_id=lead.lead_id,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        status=lead.status,
        stage=lead.stage,
        qualification_score=lead.qualification_score,
        assigned_agent_id=lead.assigned_agent_id,
        source=lead.source,
        converted_to_contact_id=lead.converted_to_contact_id,
        created_at=lead.created_at,
    )


_EXPORT_COLUMNS = (
    "lead_id",
    "name",
    "email",
    "phone",
    "status",
    "stage",
    "qualification_score",
    "assigned_agent_id",
    "source",
    "created_at",
)


def _lead_to_csv_row(lead: Lead) -> list[str]:
    return [
        lead.lead_id,
        lead.name or "",
        lead.email or "",
        lead.phone or "",
        lead.status,
        lead.stage,
        str(lead.qualification_score) if lead.qualification_score is not None else "",
        lead.assigned_agent_id or "",
        lead.source,
        lead.created_at.isoformat(),
    ]


async def _list_leads(
    request: Request,
    claims: AuthClaims,
    *,
    limit: int,
    offset: int,
    stage: str | None,
    status_: str | None,
    assigned_agent_id: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    q: str | None,
    sort: str | None,
    direction: str | None,
    include_converted: bool = False,
) -> LeadListResponse:
    """List/filter the caller's tenant leads, newest first, paginated (S12.4).

    ``stage``/``status`` are validated against ``api.leads.pipeline``'s
    canonical sets (422 ``INVALID_LEAD_FILTER`` on an unknown value).
    ``assigned_agent_id`` is an exact-match pass-through (no matching lead ->
    an honest empty page). ``from``/``to`` filter ``created_at`` as a
    half-open ``[from, to)`` window (422 ``INVALID_LIST_WINDOW`` if
    ``from >= to``). ``limit`` is clamped to ``[1, 200]``, ``offset`` to
    ``>= 0`` -- identical clamp to ``list_audit``.

    SR-25 validates a closed ``sort``/``dir`` allowlist and a combined,
    literal Name/Email ``q`` search. ``include_converted`` (SR-9.2 D1)
    defaults to ``False`` -- a lead tombstoned by conversion is excluded from
    the working list unless the caller explicitly asks to see conversion
    history.
    """
    db = request.app.state.db

    if stage is not None and stage not in _VALID_STAGES:
        raise ValidationError(
            f"Unknown stage filter {stage!r}.", code="INVALID_LEAD_FILTER",
        )
    if status_ is not None and status_ not in _VALID_STATUSES:
        raise ValidationError(
            f"Unknown status filter {status_!r}.", code="INVALID_LEAD_FILTER",
        )
    if created_from is not None and created_to is not None and created_from >= created_to:
        raise ValidationError(
            "`from` must be earlier than `to`.", code="INVALID_LIST_WINDOW",
        )

    if sort is None:
        effective_sort = "created"
    elif sort in LEAD_SORT_KEYS:
        effective_sort = sort
    else:
        raise ValidationError(f"Unknown sort key {sort!r}.", code="INVALID_LEAD_SORT")

    if direction is None:
        effective_direction = LEAD_SORT_DEFAULT_DIRECTIONS[effective_sort]
    elif direction in {"asc", "desc"}:
        effective_direction = direction
    else:
        raise ValidationError(
            f"Unknown sort direction {direction!r}.", code="INVALID_LEAD_SORT_DIRECTION",
        )

    normalized_q = q.strip() if q is not None else None
    if normalized_q == "":
        normalized_q = None
    elif normalized_q is not None and not 2 <= len(normalized_q) <= 200:
        raise ValidationError(
            "Lead search must contain between 2 and 200 characters.", code="INVALID_LEAD_SEARCH",
        )

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)

    leads, total = await list_leads(
        db,
        claims,
        limit=clamped_limit,
        offset=clamped_offset,
        stage=stage,
        status=status_,
        assigned_agent_id=assigned_agent_id,
        created_from=created_from,
        created_to=created_to,
        include_converted=include_converted,
        q=normalized_q,
        sort=effective_sort,
        direction=effective_direction,
    )

    _log.info(
        "leads listed",
        extra={
            "event": "leads_listed",
            "tenant_id": claims.tenant_id,
            "filter_keys": sorted(
                k
                for k, v in {
                    "stage": stage,
                    "status": status_,
                    "assigned_agent_id": assigned_agent_id,
                    "from": created_from,
                    "to": created_to,
                    "q": normalized_q,
                }.items()
                if v is not None
            ),
            "sort": effective_sort,
            "direction": effective_direction,
            "result_count": len(leads),
        },
    )

    return LeadListResponse(
        items=[_to_list_item(lead) for lead in leads],
        total=total,
        limit=clamped_limit,
        offset=clamped_offset,
    )


@router.get("")
async def list_leads_route(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    stage: str | None = Query(default=None),
    status_: str | None = Query(default=None, alias="status"),
    assigned_agent_id: str | None = Query(default=None),
    created_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    created_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    q: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    direction: str | None = Query(default=None, alias="dir"),
    include_converted: bool = Query(default=False),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadListResponse:
    return await _list_leads(
        request, claims,
        limit=limit, offset=offset, stage=stage, status_=status_,
        assigned_agent_id=assigned_agent_id,
        created_from=created_from, created_to=created_to,
        q=q, sort=sort, direction=direction,
        include_converted=include_converted,
    )


@tenant_scoped_router.get("")
async def list_leads_route_for_tenant(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    stage: str | None = Query(default=None),
    status_: str | None = Query(default=None, alias="status"),
    assigned_agent_id: str | None = Query(default=None),
    created_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    created_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    q: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    direction: str | None = Query(default=None, alias="dir"),
    include_converted: bool = Query(default=False),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadListResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/leads`` (S12.7)."""
    return await _list_leads(
        request, claims,
        limit=limit, offset=offset, stage=stage, status_=status_,
        assigned_agent_id=assigned_agent_id,
        created_from=created_from, created_to=created_to,
        q=q, sort=sort, direction=direction,
        include_converted=include_converted,
    )


async def _export_leads(
    request: Request,
    claims: AuthClaims,
) -> StreamingResponse:
    """Stream a tenant-scoped CSV export of leads (S7.4 decision 5).

    Columns: ``lead_id, name, email, phone, status, stage,
    qualification_score, assigned_agent_id, source, created_at``. Consent
    text is intentionally excluded (contains free-text PII). Cross-tenant
    leads never appear -- ``list_leads_for_export`` filters by
    ``claims.tenant_id``. Logs a PII-free export event (row count only).

    NOTE: this route is registered ABOVE ``GET /{lead_id}`` -- FastAPI
    matches routes in declaration order, and ``/export`` would otherwise be
    swallowed by the ``{lead_id}`` path parameter.
    """
    db = request.app.state.db

    leads = await list_leads_for_export(db, claims)

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        writer.writerow(_EXPORT_COLUMNS)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        for lead in leads:
            writer.writerow(_lead_to_csv_row(lead))
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    _log.info(
        "lead export",
        extra={
            "event": "lead_export",
            "row_count": len(leads),
            "tenant_id": claims.tenant_id,
        },
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


@router.get("/export")
async def export_leads(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> StreamingResponse:
    return await _export_leads(request, claims)


@tenant_scoped_router.get("/export")
async def export_leads_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> StreamingResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/leads/export`` (S12.7).

    NOTE: registered ABOVE ``GET /{lead_id}`` on ``tenant_scoped_router`` too,
    for the same declaration-order reason as the implicit route.
    """
    return await _export_leads(request, claims)


async def _get_lead_detail(
    lead_id: str, request: Request, claims: AuthClaims,
) -> LeadDetailResponse:
    """Fetch a lead's pipeline detail. Returns 404 if missing or cross-tenant."""
    db = request.app.state.db

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    return _to_response(lead)


@router.get("/{lead_id}")
async def get_lead_detail(
    lead_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadDetailResponse:
    return await _get_lead_detail(lead_id, request, claims)


@tenant_scoped_router.get("/{lead_id}")
async def get_lead_detail_for_tenant(
    lead_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadDetailResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/leads/{lead_id}`` (S12.7)."""
    return await _get_lead_detail(lead_id, request, claims)


async def _patch_lead_stage(
    lead_id: str, body: LeadStageUpdateRequest, request: Request, claims: AuthClaims,
) -> LeadDetailResponse:
    """Move a lead to a new pipeline stage.

    Flow: ``get_lead`` (404 if missing/cross-tenant) -> ``validate_transition``
    (422 ``INVALID_STAGE_TRANSITION`` if illegal; nothing persisted) ->
    derive ``status`` + recompute ``qualification_score`` -> persist via
    ``update_lead_stage``. ``status`` and ``qualification_score`` are always
    derived server-side -- callers never set them directly.
    """
    db = request.app.state.db

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    validate_transition(lead.stage, body.stage)

    new_status = status_for_stage(body.stage)
    scored_lead = Lead(
        lead_id=lead.lead_id,
        visitor_id=lead.visitor_id,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        status=new_status,
        stage=body.stage,
        qualification_score=lead.qualification_score,
        consent=lead.consent,
        assigned_agent_id=lead.assigned_agent_id,
        source=lead.source,
        created_at=lead.created_at,
        updated_at=lead.updated_at,
    )
    new_score = compute_qualification_score(scored_lead)

    updated = await update_lead_stage(
        db,
        claims,
        lead_id,
        stage=body.stage,
        status=new_status,
        qualification_score=new_score,
    )
    if updated is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    await add_activity(
        db,
        claims,
        lead_id,
        type="stage_change",
        payload={"from_stage": lead.stage, "to_stage": body.stage},
        actor=claims.subject,
    )

    await record_audit(
        db,
        claims,
        action="lead_stage_transitioned",
        target_type="lead",
        target_id=lead_id,
        metadata={"from_stage": lead.stage, "to_stage": body.stage},
        actor_context=get_platform_admin_actor(request),
    )

    # PII-safe transition log: lead_id/tenant_id/from_stage/to_stage/event only.
    _log.info(
        "lead stage transitioned",
        extra={
            "event": "lead_stage_transitioned",
            "lead_id": lead_id,
            "tenant_id": claims.tenant_id,
            "from_stage": lead.stage,
            "to_stage": body.stage,
        },
    )

    # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable stage write
    # and audit row above. payload is ids/stage labels only -- never PII.
    await emit_event_safe(
        db,
        claims,
        kind="lead_stage_transitioned",
        category="leads",
        target_type="lead",
        target_id=lead_id,
        payload={"lead_id": lead_id, "from_stage": lead.stage, "to_stage": body.stage},
        actor_id=claims.subject,
    )

    return _to_response(updated)


@router.patch("/{lead_id}")
async def patch_lead_stage(
    lead_id: str,
    body: LeadStageUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadDetailResponse:
    return await _patch_lead_stage(lead_id, body, request, claims)


@tenant_scoped_router.patch("/{lead_id}")
async def patch_lead_stage_for_tenant(
    lead_id: str,
    body: LeadStageUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadDetailResponse:
    """PLATFORM_ADMIN super-user variant of ``PATCH /admin/leads/{lead_id}`` (S12.7)."""
    return await _patch_lead_stage(lead_id, body, request, claims)


async def _post_lead_note(
    lead_id: str, body: LeadNoteRequest, request: Request, claims: AuthClaims,
) -> LeadActivityResponse:
    """Append a free-text note to a lead's timeline.

    404 if the lead is missing or cross-tenant; 422 if the note is blank or
    exceeds the length bound (enforced by ``LeadNoteRequest``). Note text may
    contain PII -- it is stored, never logged.
    """
    db = request.app.state.db

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    activity_id = await add_activity(
        db,
        claims,
        lead_id,
        type="note",
        payload={"text": body.text},
        actor=claims.subject,
    )

    # Audit metadata is PII-free (note text is intentionally excluded).
    await record_audit(
        db,
        claims,
        action="lead_note_added",
        target_type="lead",
        target_id=lead_id,
        metadata={"activity_id": activity_id},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "lead note added",
        extra={
            "event": "lead_note_added",
            "activity_id": activity_id,
            "lead_id": lead_id,
            "tenant_id": claims.tenant_id,
            "type": "note",
        },
    )

    return LeadActivityResponse(
        activity_id=activity_id,
        lead_id=lead_id,
        type="note",
        payload={"text": body.text},
        actor=claims.subject,
        created_at=datetime.now(tz=UTC),
    )


@router.post("/{lead_id}/notes", status_code=status.HTTP_201_CREATED)
async def post_lead_note(
    lead_id: str,
    body: LeadNoteRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadActivityResponse:
    return await _post_lead_note(lead_id, body, request, claims)


@tenant_scoped_router.post("/{lead_id}/notes", status_code=status.HTTP_201_CREATED)
async def post_lead_note_for_tenant(
    lead_id: str,
    body: LeadNoteRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadActivityResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/leads/{lead_id}/notes`` (S12.7)."""
    return await _post_lead_note(lead_id, body, request, claims)


async def _post_lead_assignment(
    lead_id: str, body: LeadAssignmentRequest, request: Request, claims: AuthClaims,
) -> LeadDetailResponse:
    """Assign a lead to a same-tenant, active ``CLIENT_AGENT``.

    404 if the lead is missing/cross-tenant. The assignee is validated via
    ``auth.repository.get_user_by_id`` -- a not-found, cross-tenant,
    wrong-role, or inactive assignee are all indistinguishable 422
    ``INVALID_ASSIGNEE`` responses (never leak whether a user id exists in
    another tenant). On success, appends an ``assignment`` activity.
    """
    db = request.app.state.db

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    assignee = await get_user_by_id(db, body.agent_id)
    if (
        assignee is None
        or assignee.get("tenant_id") != claims.tenant_id
        or not assignee.get("active")
        or assignee.get("role") != Role.CLIENT_AGENT.value
    ):
        raise ValidationError(
            "The specified assignee is not a valid, active agent in this tenant.",
            code="INVALID_ASSIGNEE",
        )

    updated = await assign_lead(db, claims, lead_id, agent_id=body.agent_id)
    if updated is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    activity_id = await add_activity(
        db,
        claims,
        lead_id,
        type="assignment",
        payload={"agent_id": body.agent_id, "previous_agent_id": lead.assigned_agent_id},
        actor=claims.subject,
    )

    await record_audit(
        db,
        claims,
        action="lead_assigned",
        target_type="lead",
        target_id=lead_id,
        metadata={"agent_id": body.agent_id, "previous_agent_id": lead.assigned_agent_id},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "lead assigned",
        extra={
            "event": "lead_assigned",
            "activity_id": activity_id,
            "lead_id": lead_id,
            "tenant_id": claims.tenant_id,
            "type": "assignment",
        },
    )

    # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable assignment
    # write and audit row above. payload is ids only -- never PII.
    await emit_event_safe(
        db,
        claims,
        kind="lead_assigned",
        category="leads",
        target_type="lead",
        target_id=lead_id,
        payload={"lead_id": lead_id, "agent_id": body.agent_id},
        actor_id=claims.subject,
    )

    return _to_response(updated)


@router.post("/{lead_id}/assignment")
async def post_lead_assignment(
    lead_id: str,
    body: LeadAssignmentRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadDetailResponse:
    return await _post_lead_assignment(lead_id, body, request, claims)


@tenant_scoped_router.post("/{lead_id}/assignment")
async def post_lead_assignment_for_tenant(
    lead_id: str,
    body: LeadAssignmentRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> LeadDetailResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/leads/{lead_id}/assignment`` (S12.7)."""
    return await _post_lead_assignment(lead_id, body, request, claims)


async def _get_lead_activities(
    lead_id: str, request: Request, claims: AuthClaims,
) -> list[LeadActivityResponse]:
    """Fetch a lead's timeline, newest first. 404 if missing or cross-tenant."""
    db = request.app.state.db

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    activities = await list_activities(db, claims, lead_id)
    return [_to_activity_response(a) for a in activities]


@router.get("/{lead_id}/activities")
async def get_lead_activities(
    lead_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> list[LeadActivityResponse]:
    return await _get_lead_activities(lead_id, request, claims)


@tenant_scoped_router.get("/{lead_id}/activities")
async def get_lead_activities_for_tenant(
    lead_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> list[LeadActivityResponse]:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/leads/{lead_id}/activities`` (S12.7)."""
    return await _get_lead_activities(lead_id, request, claims)


# ---------------------------------------------------------------------------
# POST /admin/leads/{lead_id}/convert (SR-9.2)
# ---------------------------------------------------------------------------


class LeadConvertRequest(BaseModel):
    """Body for POST /admin/leads/{lead_id}/convert.

    ``account_id``: validate a supplied same-tenant Account (422
    ``INVALID_ACCOUNT`` if it does not belong to this tenant).
    ``account_name``: create a NEW Account with this name and link it, as an
    alternative to supplying an existing ``account_id``. Both are optional
    and mutually exclusive-in-practice (if both given, ``account_id`` wins);
    neither is required -- D3, an Account is always optional.
    """

    account_id: str | None = None
    account_name: str | None = None


class LeadConvertResponse(BaseModel):
    """Leak-free response body for POST /admin/leads/{lead_id}/convert."""

    contact_id: str
    lead_id: str


def _already_converted_response(contact_id: str) -> JSONResponse:
    """Build the 409 ``LEAD_ALREADY_CONVERTED`` body (D6).

    ``AppException.to_dict()`` deliberately never serializes ``details`` to
    the client (server-side context only, per ``common.errors``), but D6
    requires the 409 body to carry the existing ``contact_id`` so the caller
    has a pointer, not just an error code -- so this path builds the JSON
    response directly rather than raising ``ConflictError``.
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error_code": "LEAD_ALREADY_CONVERTED",
            "message": "This lead has already been converted to a contact.",
            "contact_id": contact_id,
        },
    )


async def _convert_lead(
    lead_id: str, body: LeadConvertRequest, request: Request, claims: AuthClaims,
) -> LeadConvertResponse | JSONResponse:
    """Convert a Lead into a durable Contact (SR-9.2 D1/D5/D6/D7).

    Flow: ``get_lead`` (404 if missing/cross-tenant) -> 409
    ``LEAD_ALREADY_CONVERTED`` (with the existing ``contact_id``) if
    ``converted_to_contact_id`` is already set -> optional Account
    resolution (create-if-``account_name``-given, or validate a supplied
    ``account_id`` is same-tenant, 422 ``INVALID_ACCOUNT`` otherwise) ->
    ``create_contact`` copying the Lead's ``name``/``email``/``phone``/
    ``consent`` verbatim (D7 -- consent's ``captured_at`` is NOT re-stamped)
    -> ``add_identity`` rows for the lead's ``email`` and ``visitor_id``
    where present (D4; skipped, not written NULL/empty, when absent) ->
    ``mark_lead_converted`` (the D5/D6 schema-and-guard-backed tombstone) ->
    ``add_activity(type="converted_to_contact")`` -> ``record_audit``.

    This is a **separate lifecycle action** that bypasses
    ``api.leads.pipeline.validate_transition`` entirely (D5) -- conversion
    succeeds directly from ``stage='captured'``, the state every SR-9.1
    booking-created lead starts in, which the ordinary pipeline validator
    would reject.
    """
    db = request.app.state.db

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    if lead.converted_to_contact_id is not None:
        return _already_converted_response(lead.converted_to_contact_id)

    # -- Optional Account resolution (D3: never required) -------------------
    account_id = body.account_id
    if account_id is not None:
        account = await get_account(db, claims, account_id)
        if account is None:
            raise ValidationError(
                "The specified account does not exist in this tenant.",
                code="INVALID_ACCOUNT",
            )
    elif body.account_name is not None:
        from api.accounts.repository import create_account  # noqa: PLC0415

        account_id = await create_account(db, claims, name=body.account_name)

    # -- Create the Contact, carrying the Lead's consent verbatim (D7) ------
    contact_id = await create_contact(
        db,
        claims,
        account_id=account_id,
        lead_id=lead_id,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        consent=lead.consent,
    )

    # -- Identity rows (D4): email + visitor_id where present, never NULL ---
    if lead.email:
        await add_contact_identity(
            db, claims, contact_id, identity_type="email", identity_value=lead.email,
        )
    if lead.visitor_id:
        await add_contact_identity(
            db, claims, contact_id, identity_type="visitor_id", identity_value=lead.visitor_id,
        )

    # -- Tombstone the Lead (D1/D5/D6) ---------------------------------------
    converted = await mark_lead_converted(db, claims, lead_id, contact_id=contact_id)
    if not converted:
        # Lost a race against a concurrent convert -- the partial unique
        # index on contacts(tenant_id, lead_id) already stopped a duplicate
        # Contact from being created (D6); report it honestly rather than a
        # silent 200 with a Contact the caller's own request produced but
        # that lost the tombstone race.
        return _already_converted_response(contact_id)

    await add_activity(
        db,
        claims,
        lead_id,
        type="converted_to_contact",
        payload={"contact_id": contact_id},
        actor=claims.subject,
    )

    await record_audit(
        db,
        claims,
        action="lead_converted_to_contact",
        target_type="lead",
        target_id=lead_id,
        metadata={"contact_id": contact_id},
        actor_context=get_platform_admin_actor(request),
    )

    # PII-safe conversion log: lead_id/contact_id/tenant_id/event only.
    _log.info(
        "lead converted to contact",
        extra={
            "event": "lead_converted_to_contact",
            "lead_id": lead_id,
            "contact_id": contact_id,
            "tenant_id": claims.tenant_id,
        },
    )

    # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable tombstone
    # write and audit row above. payload is ids only -- never PII.
    await emit_event_safe(
        db,
        claims,
        kind="lead_converted",
        category="leads",
        target_type="lead",
        target_id=lead_id,
        payload={"lead_id": lead_id, "contact_id": contact_id},
        actor_id=claims.subject,
    )

    return LeadConvertResponse(contact_id=contact_id, lead_id=lead_id)


@router.post("/{lead_id}/convert", status_code=status.HTTP_201_CREATED, response_model=None)
async def convert_lead(
    lead_id: str,
    body: LeadConvertRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> LeadConvertResponse | JSONResponse:
    return await _convert_lead(lead_id, body, request, claims)


@tenant_scoped_router.post(
    "/{lead_id}/convert", status_code=status.HTTP_201_CREATED, response_model=None,
)
async def convert_lead_for_tenant(
    lead_id: str,
    body: LeadConvertRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> LeadConvertResponse | JSONResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/leads/{lead_id}/convert``."""
    return await _convert_lead(lead_id, body, request, claims)
