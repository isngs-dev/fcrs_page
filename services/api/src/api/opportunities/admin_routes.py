"""Admin/agent opportunity routes -- POST/GET /admin/opportunities,
GET/PATCH /admin/opportunities/{id}, POST /admin/opportunities/{id}/stage,
GET/PUT /admin/opportunities/config.

RBAC per SR-9.4 D8 (a DELIBERATE divergence from SR-9.2's read-only
contacts shape): ``CLIENT_ADMIN`` full access everywhere; ``CLIENT_AGENT``
full working access on opportunities (create/read/update/transition-stage)
but READ-ONLY on the win-probability/currency config (403 on PUT, 200 on
GET); ``VISITOR`` rejected on everything; ``PLATFORM_ADMIN`` rejected on
the implicit routes, allowed only via the tenant-explicit
``/admin/tenants/{tenant_id}/opportunities`` family, mirroring
``contacts/admin_routes.py``/``leads/admin_routes.py`` exactly.

Every access is tenant-scoped via ``claims.tenant_id`` -- a cross-tenant
``opportunity_id`` is indistinguishable from a missing one and returns 404
(no existence leak). No DELETE endpoint this increment (D8). No public/
visitor endpoint at all -- Opportunities are never reachable from the
widget.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request, status

from api.accounts.repository import get_account
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.contacts.repository import get_contact
from api.opportunities.config_repository import (
    OpportunityConfig,
    get_opportunity_config,
    upsert_opportunity_config,
)
from api.opportunities.models import (
    OpportunityConfigResponse,
    OpportunityConfigUpdateRequest,
    OpportunityCreateRequest,
    OpportunityListResponse,
    OpportunityResponse,
    OpportunityStageTransitionRequest,
    OpportunityUpdateRequest,
)
from api.opportunities.pipeline import validate_transition, win_probability_for_stage
from api.opportunities.repository import (
    Opportunity,
    create_opportunity,
    get_opportunity,
    list_opportunities,
    transition_stage,
    update_opportunity,
)

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/opportunities", tags=["opportunities"])
tenant_scoped_router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/opportunities", tags=["opportunities"]
)


def _to_response(opportunity: Opportunity, config: OpportunityConfig) -> OpportunityResponse:
    return OpportunityResponse(
        opportunity_id=opportunity.opportunity_id,
        contact_id=opportunity.contact_id,
        account_id=opportunity.account_id,
        name=opportunity.name,
        amount=opportunity.amount,
        currency=opportunity.currency,
        stage=opportunity.stage,
        win_probability=win_probability_for_stage(opportunity.stage, config),
        expected_close_date=opportunity.expected_close_date,
        closed_at=opportunity.closed_at,
        close_reason=opportunity.close_reason,
        owner_agent_id=opportunity.owner_agent_id,
        created_at=opportunity.created_at,
    )


async def _validate_account_id(db: Any, claims: AuthClaims, account_id: str | None) -> None:
    """Raise 422 ``INVALID_ACCOUNT`` if ``account_id`` is supplied but does
    not belong to the caller's tenant."""
    if account_id is None:
        return
    account = await get_account(db, claims, account_id)
    if account is None:
        raise ValidationError(
            "The specified account does not exist in this tenant.",
            code="INVALID_ACCOUNT",
        )


# ---------------------------------------------------------------------------
# POST /admin/opportunities
# ---------------------------------------------------------------------------


async def _post_opportunity(
    body: OpportunityCreateRequest, request: Request, claims: AuthClaims,
) -> OpportunityResponse:
    """Create an Opportunity manually (D4 -- the ONLY creation path; nothing
    auto-creates one).

    Flow: validate ``contact_id`` is same-tenant (422 ``INVALID_CONTACT``
    otherwise) -> validate a supplied ``account_id`` is same-tenant (422
    ``INVALID_ACCOUNT`` otherwise) -> when ``account_id`` is omitted, COPY
    the contact's current ``account_id`` onto the row (a snapshot, D6) ->
    stamp ``currency`` from ``get_opportunity_config`` (D7) -> insert at
    ``stage='prospecting'`` (D1).
    """
    db = request.app.state.db

    if body.amount is not None and body.amount < 0:
        raise ValidationError("amount must be >= 0.", code="INVALID_AMOUNT")

    contact = await get_contact(db, claims, body.contact_id)
    if contact is None:
        raise ValidationError(
            "The specified contact does not exist in this tenant.",
            code="INVALID_CONTACT",
        )

    await _validate_account_id(db, claims, body.account_id)

    # D6: omitted account_id copies the contact's current account_id (which
    # may itself be None) -- a snapshot, not a live inheritance.
    account_id = body.account_id if body.account_id is not None else contact.account_id

    config = await get_opportunity_config(db, claims)

    opportunity_id = await create_opportunity(
        db,
        claims,
        contact_id=body.contact_id,
        account_id=account_id,
        name=body.name,
        amount=body.amount,
        currency=config.currency,
        expected_close_date=body.expected_close_date,
        owner_agent_id=body.owner_agent_id,
    )

    await record_audit(
        db,
        claims,
        action="opportunity_created",
        target_type="opportunity",
        target_id=opportunity_id,
        metadata={"contact_id": body.contact_id, "account_id": account_id},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "opportunity created",
        extra={
            "event": "opportunity_created",
            "tenant_id": claims.tenant_id,
            "opportunity_id": opportunity_id,
            "contact_id": body.contact_id,
            "stage": "prospecting",
        },
    )

    opportunity = await get_opportunity(db, claims, opportunity_id)
    if opportunity is None:
        raise NotFoundError("Opportunity not found immediately after creation.", code="NOT_FOUND")
    return _to_response(opportunity, config)


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_opportunity(
    body: OpportunityCreateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    return await _post_opportunity(body, request, claims)


@tenant_scoped_router.post("", status_code=status.HTTP_201_CREATED)
async def post_opportunity_for_tenant(
    body: OpportunityCreateRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/opportunities``."""
    return await _post_opportunity(body, request, claims)


# ---------------------------------------------------------------------------
# GET /admin/opportunities (list)
# ---------------------------------------------------------------------------


async def _list_opportunities(
    request: Request,
    claims: AuthClaims,
    *,
    limit: int,
    offset: int,
    stage: str | None,
    contact_id: str | None,
    account_id: str | None,
    owner_agent_id: str | None,
    open_only: bool,
) -> OpportunityListResponse:
    db = request.app.state.db

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)

    opportunities, total = await list_opportunities(
        db,
        claims,
        limit=clamped_limit,
        offset=clamped_offset,
        stage=stage,
        contact_id=contact_id,
        account_id=account_id,
        owner_agent_id=owner_agent_id,
        open_only=open_only,
    )
    config = await get_opportunity_config(db, claims)

    _log.info(
        "opportunities listed",
        extra={
            "event": "opportunities_listed",
            "tenant_id": claims.tenant_id,
            "result_count": len(opportunities),
        },
    )

    return OpportunityListResponse(
        items=[_to_response(o, config) for o in opportunities],
        total=total,
        limit=clamped_limit,
        offset=clamped_offset,
    )


@router.get("")
async def list_opportunities_route(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    stage: str | None = Query(default=None),
    contact_id: str | None = Query(default=None),
    account_id: str | None = Query(default=None),
    owner_agent_id: str | None = Query(default=None),
    open_only: bool = Query(default=False),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityListResponse:
    return await _list_opportunities(
        request, claims, limit=limit, offset=offset, stage=stage,
        contact_id=contact_id, account_id=account_id,
        owner_agent_id=owner_agent_id, open_only=open_only,
    )


@tenant_scoped_router.get("")
async def list_opportunities_route_for_tenant(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    stage: str | None = Query(default=None),
    contact_id: str | None = Query(default=None),
    account_id: str | None = Query(default=None),
    owner_agent_id: str | None = Query(default=None),
    open_only: bool = Query(default=False),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityListResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/opportunities``."""
    return await _list_opportunities(
        request, claims, limit=limit, offset=offset, stage=stage,
        contact_id=contact_id, account_id=account_id,
        owner_agent_id=owner_agent_id, open_only=open_only,
    )


# ---------------------------------------------------------------------------
# GET /admin/opportunities/{id}
# ---------------------------------------------------------------------------


async def _get_opportunity_detail(
    opportunity_id: str, request: Request, claims: AuthClaims,
) -> OpportunityResponse:
    """Fetch an opportunity's detail. Returns 404 if missing or cross-tenant."""
    db = request.app.state.db

    opportunity = await get_opportunity(db, claims, opportunity_id)
    if opportunity is None:
        raise NotFoundError("Opportunity not found.", code="NOT_FOUND")

    config = await get_opportunity_config(db, claims)
    return _to_response(opportunity, config)


@router.get("/config")
async def get_opportunity_config_route(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityConfigResponse:
    return await _get_config(request, claims)


@tenant_scoped_router.get("/config")
async def get_opportunity_config_route_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityConfigResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/opportunities/config``."""
    return await _get_config(request, claims)


@router.get("/{opportunity_id}")
async def get_opportunity_detail(
    opportunity_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    return await _get_opportunity_detail(opportunity_id, request, claims)


@tenant_scoped_router.get("/{opportunity_id}")
async def get_opportunity_detail_for_tenant(
    opportunity_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/opportunities/{opportunity_id}``."""
    return await _get_opportunity_detail(opportunity_id, request, claims)


# ---------------------------------------------------------------------------
# PATCH /admin/opportunities/{id}
# ---------------------------------------------------------------------------


async def _patch_opportunity(
    opportunity_id: str, body: OpportunityUpdateRequest, request: Request, claims: AuthClaims,
) -> OpportunityResponse:
    """Partially update an opportunity. 404 if missing/cross-tenant.

    Only fields present in the JSON body are changed (``exclude_unset``);
    an explicit ``expected_close_date: null`` clears it. A supplied
    ``account_id`` from another tenant is rejected 422 ``INVALID_ACCOUNT``.
    Deliberately excludes ``stage`` and ``currency`` -- those are immutable
    via PATCH (D1/D7).
    """
    db = request.app.state.db

    existing = await get_opportunity(db, claims, opportunity_id)
    if existing is None:
        raise NotFoundError("Opportunity not found.", code="NOT_FOUND")

    fields = body.model_dump(exclude_unset=True)

    if "account_id" in fields:
        await _validate_account_id(db, claims, fields["account_id"])

    update_kwargs: dict[str, Any] = {}
    for key in ("name", "amount", "expected_close_date", "owner_agent_id", "account_id"):
        if key in fields:
            update_kwargs[key] = fields[key]

    new_amount = update_kwargs.get("amount")
    if "amount" in update_kwargs and new_amount is not None and new_amount < 0:
        raise ValidationError("amount must be >= 0.", code="INVALID_AMOUNT")

    if update_kwargs:
        updated = await update_opportunity(db, claims, opportunity_id, **update_kwargs)
        if updated is None:
            raise NotFoundError("Opportunity not found.", code="NOT_FOUND")
    else:
        updated = existing

    await record_audit(
        db,
        claims,
        action="opportunity_updated",
        target_type="opportunity",
        target_id=opportunity_id,
        metadata={"updated_fields": sorted(update_kwargs.keys())},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "opportunity updated",
        extra={
            "event": "opportunity_updated",
            "tenant_id": claims.tenant_id,
            "opportunity_id": opportunity_id,
            "stage": updated.stage,
        },
    )

    config = await get_opportunity_config(db, claims)
    return _to_response(updated, config)


@router.patch("/{opportunity_id}")
async def patch_opportunity(
    opportunity_id: str,
    body: OpportunityUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    return await _patch_opportunity(opportunity_id, body, request, claims)


@tenant_scoped_router.patch("/{opportunity_id}")
async def patch_opportunity_for_tenant(
    opportunity_id: str,
    body: OpportunityUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    """PLATFORM_ADMIN super-user variant of ``PATCH /admin/opportunities/{opportunity_id}``."""
    return await _patch_opportunity(opportunity_id, body, request, claims)


# ---------------------------------------------------------------------------
# POST /admin/opportunities/{id}/stage
# ---------------------------------------------------------------------------


async def _post_stage_transition(
    opportunity_id: str,
    body: OpportunityStageTransitionRequest,
    request: Request,
    claims: AuthClaims,
) -> OpportunityResponse:
    """Move an opportunity to a new stage.

    Flow: ``get_opportunity`` (404 if missing/cross-tenant) ->
    ``validate_transition`` (422 ``INVALID_OPPORTUNITY_STAGE_TRANSITION`` if
    illegal; nothing persisted) -> 422 ``CLOSE_REASON_REQUIRED`` if
    transitioning to ``closed_lost`` without a non-empty, non-whitespace
    ``close_reason`` (D9; nothing persisted) -> stamp ``closed_at``
    server-side on any terminal transition -> persist via
    ``transition_stage``. There is NO reopen path anywhere (D5).
    """
    db = request.app.state.db

    opportunity = await get_opportunity(db, claims, opportunity_id)
    if opportunity is None:
        raise NotFoundError("Opportunity not found.", code="NOT_FOUND")

    validate_transition(opportunity.stage, body.stage)

    if body.stage == "closed_lost" and (
        body.close_reason is None or not body.close_reason.strip()
    ):
        raise ValidationError(
            "close_reason is required when transitioning to closed_lost.",
            code="CLOSE_REASON_REQUIRED",
        )

    is_terminal = body.stage in ("closed_won", "closed_lost")
    closed_at = datetime.now(UTC) if is_terminal else None

    updated = await transition_stage(
        db,
        claims,
        opportunity_id,
        stage=body.stage,
        close_reason=body.close_reason,
        closed_at=closed_at,
    )
    if updated is None:
        raise NotFoundError("Opportunity not found.", code="NOT_FOUND")

    await record_audit(
        db,
        claims,
        action="opportunity_stage_transitioned",
        target_type="opportunity",
        target_id=opportunity_id,
        metadata={"from_stage": opportunity.stage, "to_stage": body.stage},
        actor_context=get_platform_admin_actor(request),
    )

    # PII-safe transition log: never log close_reason (free text an agent
    # may paste a customer quote or sensitive detail into).
    _log.info(
        "opportunity stage transitioned",
        extra={
            "event": "opportunity_stage_transitioned",
            "tenant_id": claims.tenant_id,
            "opportunity_id": opportunity_id,
            "stage": body.stage,
        },
    )

    config = await get_opportunity_config(db, claims)
    return _to_response(updated, config)


@router.post("/{opportunity_id}/stage")
async def post_stage_transition(
    opportunity_id: str,
    body: OpportunityStageTransitionRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    return await _post_stage_transition(opportunity_id, body, request, claims)


@tenant_scoped_router.post("/{opportunity_id}/stage")
async def post_stage_transition_for_tenant(
    opportunity_id: str,
    body: OpportunityStageTransitionRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> OpportunityResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/opportunities/{id}/stage``."""
    return await _post_stage_transition(opportunity_id, body, request, claims)


# ---------------------------------------------------------------------------
# GET / PUT /admin/opportunities/config
# ---------------------------------------------------------------------------


async def _get_config(request: Request, claims: AuthClaims) -> OpportunityConfigResponse:
    db = request.app.state.db
    config = await get_opportunity_config(db, claims)
    return OpportunityConfigResponse(
        currency=config.currency, stage_probabilities=config.stage_probabilities,
    )


async def _put_config(
    body: OpportunityConfigUpdateRequest, request: Request, claims: AuthClaims,
) -> OpportunityConfigResponse:
    """Update the caller's tenant opportunity config (D8: CLIENT_ADMIN only).

    Validation (``INVALID_CURRENCY``/``INVALID_STAGE_PROBABILITIES``) happens
    inside ``upsert_opportunity_config`` before the DB is touched.
    """
    db = request.app.state.db

    await upsert_opportunity_config(
        db,
        claims,
        currency=body.currency,
        stage_probabilities=body.stage_probabilities,
    )

    await record_audit(
        db,
        claims,
        action="opportunity_config_updated",
        target_type="opportunity_config",
        target_id=claims.tenant_id,
        metadata={"currency": body.currency},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "opportunity config updated",
        extra={"event": "opportunity_config_updated", "tenant_id": claims.tenant_id},
    )

    config = await get_opportunity_config(db, claims)
    return OpportunityConfigResponse(
        currency=config.currency, stage_probabilities=config.stage_probabilities,
    )


@router.put("/config")
async def put_opportunity_config_route(
    body: OpportunityConfigUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> OpportunityConfigResponse:
    return await _put_config(body, request, claims)


@tenant_scoped_router.put("/config")
async def put_opportunity_config_route_for_tenant(
    body: OpportunityConfigUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> OpportunityConfigResponse:
    """PLATFORM_ADMIN super-user variant of ``PUT /admin/opportunities/config``."""
    return await _put_config(body, request, claims)
