"""API-keys routes -- GET /admin/api-keys, POST /admin/api-keys/rotate,
PUT /admin/api-keys/origins (SR-20 D6).

Mirrors the paired-router + shared-``_impl`` pattern (M11): ``router`` +
``tenant_scoped_router``, both delegating to one private ``_impl`` per verb,
registered in ``app.py`` at all three sites. RBAC: ``CLIENT_ADMIN``-only
everywhere on this surface (own tenant) -- ``CLIENT_AGENT`` gets 403
(tenant-wide config, CLAUDE.md §3: agents "cannot change config").
``PLATFORM_ADMIN`` reaches a tenant only via the tenant-explicit twin
(``resolve_tenant_scope``), 404 ``TENANT_NOT_FOUND`` for an unknown tenant,
honest audit actor (D4's ``actor_context`` pattern) -- alongside, NOT
replacing, the EXISTING PLATFORM_ADMIN-only
``POST /admin/tenants/{tenant_id}/rotate-key`` route (M7, untouched).

Credential hygiene (D6, mandatory): the raw key is returned ONCE, in the
rotation response body only -- never logged, never in an audit row, never
returned by GET. ``get_api_keys``/``ApiKeyInfo`` structurally cannot leak it
(the repository layer never selects/returns the hash either).
"""
from __future__ import annotations

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api.admin.api_keys_repository import (
    ApiKeyInfo,
    get_api_key_info,
    rotate_own_key,
    update_allowed_origins,
)
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/api-keys", tags=["admin"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/api-keys", tags=["admin"])


class ApiKeyInfoResponse(BaseModel):
    """Response for GET /admin/api-keys. NEVER carries key material."""

    has_key: bool
    allowed_origins: list[str]


class RotateKeyResponse(BaseModel):
    """Response for POST /admin/api-keys/rotate.

    ``client_key`` is the raw key, returned EXACTLY ONCE -- the caller must
    copy it now; it is never retrievable again (the DB only ever stores its
    hash, M6).
    """

    client_key: str


class UpdateOriginsRequest(BaseModel):
    """Body for PUT /admin/api-keys/origins."""

    origins: list[str] = Field(default_factory=list, max_length=20)


def _to_response(info: ApiKeyInfo) -> ApiKeyInfoResponse:
    return ApiKeyInfoResponse(has_key=info.has_key, allowed_origins=info.allowed_origins)


async def _get_api_keys(request: Request, claims: AuthClaims) -> ApiKeyInfoResponse:
    db = request.app.state.db
    info = await get_api_key_info(db, claims)
    return _to_response(info)


async def _rotate(request: Request, claims: AuthClaims) -> RotateKeyResponse:
    db = request.app.state.db

    raw_key = await rotate_own_key(db, claims)

    await record_audit(
        db,
        claims,
        action="client_key_rotated_by_admin",
        target_type="tenant",
        target_id=claims.tenant_id,
        actor_context=get_platform_admin_actor(request),
    )

    # PII/secret-minimal log line -- NEVER the raw or hashed key (mirrors
    # the existing PLATFORM_ADMIN rotate route's client_key_rotated line,
    # M7).
    _log.info(
        "client key rotated by client admin",
        extra={"event": "client_key_rotated", "tenant_id": claims.tenant_id},
    )

    return RotateKeyResponse(client_key=raw_key)


async def _update_origins(
    body: UpdateOriginsRequest, request: Request, claims: AuthClaims,
) -> ApiKeyInfoResponse:
    db = request.app.state.db

    info = await update_allowed_origins(db, claims, origins=body.origins)

    await record_audit(
        db,
        claims,
        action="workspace_origins_updated",
        target_type="tenant",
        target_id=claims.tenant_id,
        metadata={"origin_count": len(body.origins)},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "workspace origin allowlist updated",
        extra={"event": "workspace_origins_updated", "tenant_id": claims.tenant_id},
    )

    return _to_response(info)


@router.get("")
async def get_api_keys_route(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ApiKeyInfoResponse:
    return await _get_api_keys(request, claims)


@router.post("/rotate")
async def rotate_route(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> RotateKeyResponse:
    return await _rotate(request, claims)


@router.put("/origins")
async def update_origins_route(
    body: UpdateOriginsRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ApiKeyInfoResponse:
    return await _update_origins(body, request, claims)


@tenant_scoped_router.get("")
async def get_api_keys_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ApiKeyInfoResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/api-keys`` (M11)."""
    return await _get_api_keys(request, claims)


@tenant_scoped_router.post("/rotate")
async def rotate_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> RotateKeyResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/api-keys/rotate``
    (M11) -- alongside, not replacing, the existing PLATFORM_ADMIN-only
    ``POST /admin/tenants/{tenant_id}/rotate-key`` route (M7)."""
    return await _rotate(request, claims)


@tenant_scoped_router.put("/origins")
async def update_origins_for_tenant(
    body: UpdateOriginsRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ApiKeyInfoResponse:
    """PLATFORM_ADMIN super-user variant of ``PUT /admin/api-keys/origins`` (M11)."""
    return await _update_origins(body, request, claims)
