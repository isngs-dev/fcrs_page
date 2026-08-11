"""Round-robin assignment-config routes -- GET/PUT /admin/assignment-config
(SR-20 D1/D2). Backs the Team-members page toggle.

Mirrors ``api.admin.settings_routes``'s exact shape (M11 -- mandatory):
paired routers (``router`` + ``tenant_scoped_router``), one shared private
``_impl`` per verb. RBAC: ``CLIENT_ADMIN``/``CLIENT_AGENT`` can GET (agents
reviewing the team screen can see whether round-robin is on); ``PUT`` is
``CLIENT_ADMIN``-only (CLAUDE.md §3: agents "cannot change config").

Delegates entirely to ``api.leads.assignment_config_repository`` -- this
module adds no new logic, only the HTTP surface + audit trail over the
existing never-``None`` resolver + validated upsert.
"""
from __future__ import annotations

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.leads.assignment_config_repository import (
    AssignmentConfig,
    get_assignment_config,
    upsert_assignment_config,
)

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/assignment-config", tags=["admin"])
tenant_scoped_router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/assignment-config", tags=["admin"]
)


class AssignmentConfigRequest(BaseModel):
    """Body for PUT /admin/assignment-config."""

    round_robin_enabled: bool


class AssignmentConfigResponse(BaseModel):
    """Response for GET/PUT /admin/assignment-config.

    Never exposes ``last_assigned_agent_id`` (the rotation cursor) -- it is
    internal state, not a setting a client admin edits or needs to see.
    """

    round_robin_enabled: bool


def _to_response(config: AssignmentConfig) -> AssignmentConfigResponse:
    return AssignmentConfigResponse(round_robin_enabled=config.round_robin_enabled)


async def _get_config(request: Request, claims: AuthClaims) -> AssignmentConfigResponse:
    db = request.app.state.db
    config = await get_assignment_config(db, claims)
    return _to_response(config)


async def _put_config(
    body: AssignmentConfigRequest, request: Request, claims: AuthClaims,
) -> AssignmentConfigResponse:
    db = request.app.state.db

    await upsert_assignment_config(db, claims, round_robin_enabled=body.round_robin_enabled)

    await record_audit(
        db,
        claims,
        action="assignment_config_updated",
        target_type="tenant",
        target_id=claims.tenant_id,
        metadata={"round_robin_enabled": body.round_robin_enabled},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "assignment config updated",
        extra={"event": "assignment_config_updated", "tenant_id": claims.tenant_id},
    )

    config = await get_assignment_config(db, claims)
    return _to_response(config)


@router.get("")
async def get_assignment_config_route(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AssignmentConfigResponse:
    return await _get_config(request, claims)


@router.put("")
async def put_assignment_config_route(
    body: AssignmentConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AssignmentConfigResponse:
    return await _put_config(body, request, claims)


@tenant_scoped_router.get("")
async def get_assignment_config_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AssignmentConfigResponse:
    """PLATFORM_ADMIN super-user variant (M11)."""
    return await _get_config(request, claims)


@tenant_scoped_router.put("")
async def put_assignment_config_for_tenant(
    body: AssignmentConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AssignmentConfigResponse:
    """PLATFORM_ADMIN super-user variant (M11)."""
    return await _put_config(body, request, claims)
