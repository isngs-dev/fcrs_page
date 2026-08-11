"""Tenant user-management routes (S12.2 + SR-25).

The implicit GET/POST ``/admin/users`` and PATCH ``/admin/users/{user_id}``
routes are ``Role.CLIENT_ADMIN``-only (CLAUDE.md: a ``CLIENT_AGENT``
"cannot change config", and user management is a config-adjacent, own-tenant
concern). ``POST /admin/users`` returns a generated temp password exactly
once, in the response body only (decision 7, mirrors S12.1's
``admin_password`` pattern) -- never logged. ``GET /admin/users`` never
includes ``password_hash``.

SR-25 also adds a read-only ``GET /admin/tenants/{tenant_id}/users`` twin for
the PLATFORM_ADMIN per-client Leads screen. It uses ``resolve_tenant_scope``
to derive CLIENT_ADMIN claims for the validated target tenant; no platform
user-management mutation routes are introduced.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import NotFoundError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from api.admin.users_repository import create_tenant_agent, list_tenant_users, set_user_active
from api.auth.dependencies import require_roles, resolve_tenant_scope

_log = get_logger(__name__)

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

router = APIRouter(prefix="/admin/users", tags=["admin"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/users", tags=["admin"])


class AdminCreateUserRequest(BaseModel):
    """Body for POST /admin/users.

    Deliberately has NO ``role`` field (decision 1) -- ``role=CLIENT_AGENT``
    is hardcoded server-side.
    """

    email: str = Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)
    name: str | None = None


class AdminSetUserActiveRequest(BaseModel):
    """Body for PATCH /admin/users/{user_id}."""

    active: bool


class AdminUserResponse(BaseModel):
    """Leak-free (no ``password_hash``) user row for the admin surface."""

    id: str
    tenant_id: str | None
    email: str
    role: str
    name: str | None
    active: bool
    last_login_at: datetime | None


class AdminCreateUserResponse(AdminUserResponse):
    """Response for POST /admin/users -- includes the one-time temp password."""

    temp_password: str


def _to_response(row: dict[str, Any]) -> AdminUserResponse:
    return AdminUserResponse(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]) if row.get("tenant_id") is not None else None,
        email=str(row["email"]),
        role=str(row["role"]),
        name=row.get("name"),
        active=bool(row["active"]),
        last_login_at=row.get("last_login_at"),
    )


@router.get("")
async def get_users(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> list[AdminUserResponse]:
    """List the caller's tenant's users. Never includes ``password_hash``."""
    return await _get_users(request, claims)


async def _get_users(request: Request, claims: AuthClaims) -> list[AdminUserResponse]:
    """Shared tenant-scoped user read for implicit and platform routes."""
    db = request.app.state.db
    rows = await list_tenant_users(db, claims)
    return [_to_response(row) for row in rows]


@tenant_scoped_router.get("")
async def get_users_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> list[AdminUserResponse]:
    """PLATFORM_ADMIN target-tenant variant of ``GET /admin/users``."""
    return await _get_users(request, claims)


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_user(
    body: AdminCreateUserRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AdminCreateUserResponse:
    """Create a new ``CLIENT_AGENT`` in the caller's tenant.

    422 ``ADMIN_EMAIL_TAKEN`` on a duplicate email. The generated temp
    password is returned exactly once, never logged.
    """
    db = request.app.state.db

    result = await create_tenant_agent(db, claims, email=body.email, name=body.name)

    _log.info(
        "tenant agent created",
        extra={
            "event": "tenant_agent_created",
            "tenant_id": result["tenant_id"],
            "user_id": result["user_id"],
            "email": result["email"],
        },
    )

    return AdminCreateUserResponse(
        id=result["user_id"],
        tenant_id=result["tenant_id"],
        email=result["email"],
        role=result["role"],
        name=result["name"],
        active=result["active"],
        last_login_at=None,
        temp_password=result["temp_password"],
    )


@router.patch("/{user_id}")
async def patch_user_active(
    user_id: str,
    body: AdminSetUserActiveRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AdminUserResponse:
    """Deactivate/reactivate a same-tenant ``CLIENT_AGENT``.

    404 ``USER_NOT_FOUND`` for a missing/cross-tenant ``user_id``. 422
    ``INVALID_TARGET_USER`` for self-targeting or a non-``CLIENT_AGENT``
    target (decisions 3-4).
    """
    db = request.app.state.db

    updated = await set_user_active(db, claims, user_id, active=body.active)
    if updated is None:
        raise NotFoundError("User not found.", code="USER_NOT_FOUND")

    _log.info(
        "tenant user reactivated" if body.active else "tenant user deactivated",
        extra={
            "event": "tenant_user_reactivated" if body.active else "tenant_user_deactivated",
            "tenant_id": claims.tenant_id,
            "user_id": user_id,
        },
    )

    return _to_response(updated)
