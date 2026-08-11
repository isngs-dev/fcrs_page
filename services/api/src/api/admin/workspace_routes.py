"""Workspace settings routes -- GET/PUT /admin/workspace (SR-20 D5).

Mirrors ``api.admin.settings_routes``'s exact shape (M11 -- mandatory):
paired routers (``router`` + ``tenant_scoped_router``), one shared private
``_impl`` per verb, registered in ``app.py`` at all three sites (imports,
implicit includes, tenant-scoped includes).

RBAC: ``GET`` is readable by ``CLIENT_ADMIN`` and ``CLIENT_AGENT`` (a
read-only view of workspace identity is reasonable for an agent); ``PUT`` is
``CLIENT_ADMIN``-only (CLAUDE.md: ``CLIENT_AGENT`` "cannot change config" --
this is tenant-wide config). ``PLATFORM_ADMIN`` is rejected on the implicit
routes and reaches a tenant via the explicit
``/admin/tenants/{tenant_id}/workspace`` twin only (``resolve_tenant_scope``),
404 ``TENANT_NOT_FOUND`` for an unknown/inactive tenant, with the real actor
recorded on the audit row (D4's ``actor_context`` pattern).

D5 amended: no ``language`` field anywhere on this surface -- it renders
disabled/coming-soon in the UI (see ``api.admin.workspace_repository``'s
module docstring for the full rationale).
"""
from __future__ import annotations

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api.admin.workspace_repository import Workspace, get_workspace, update_workspace
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/workspace", tags=["admin"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/workspace", tags=["admin"])


class AdminWorkspaceRequest(BaseModel):
    """Body for PUT /admin/workspace."""

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=63)
    timezone: str | None = None


class AdminWorkspaceResponse(BaseModel):
    """Response for GET/PUT /admin/workspace."""

    name: str
    slug: str
    timezone: str


def _to_response(workspace: Workspace) -> AdminWorkspaceResponse:
    return AdminWorkspaceResponse(
        name=workspace.name, slug=workspace.slug, timezone=workspace.timezone,
    )


async def _get_workspace(request: Request, claims: AuthClaims) -> AdminWorkspaceResponse:
    db = request.app.state.db
    workspace = await get_workspace(db, claims)
    return _to_response(workspace)


async def _put_workspace(
    body: AdminWorkspaceRequest, request: Request, claims: AuthClaims,
) -> AdminWorkspaceResponse:
    db = request.app.state.db

    workspace = await update_workspace(
        db, claims, name=body.name, slug=body.slug, timezone=body.timezone,
    )

    await record_audit(
        db,
        claims,
        action="workspace_updated",
        target_type="tenant",
        target_id=claims.tenant_id,
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "workspace updated",
        extra={"event": "workspace_updated", "tenant_id": claims.tenant_id},
    )

    return _to_response(workspace)


@router.get("")
async def get_workspace_route(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AdminWorkspaceResponse:
    return await _get_workspace(request, claims)


@router.put("")
async def put_workspace_route(
    body: AdminWorkspaceRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AdminWorkspaceResponse:
    return await _put_workspace(body, request, claims)


@tenant_scoped_router.get("")
async def get_workspace_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AdminWorkspaceResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/workspace`` (M11)."""
    return await _get_workspace(request, claims)


@tenant_scoped_router.put("")
async def put_workspace_for_tenant(
    body: AdminWorkspaceRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AdminWorkspaceResponse:
    """PLATFORM_ADMIN super-user variant of ``PUT /admin/workspace`` (M11)."""
    return await _put_workspace(body, request, claims)
