"""Admin routes -- one-shot tenant onboarding + client-key rotation (S12.1).

Both routes are PLATFORM_ADMIN-only (CLAUDE.md: "Platform operator only.") --
the one sanctioned exception to per-request tenant scoping, matching
``api.tenants.repository.TenantRepository.create``'s existing precedent.

Secrets hygiene (highest priority this sprint): the raw client key and the
(if server-generated) admin password are returned exactly once, in the
response body only. The ``tenant_onboarded``/``client_key_rotated`` log
lines are PII/secret-minimal -- ``tenant_id``/``slug``/``admin_email`` only,
NEVER a raw or hashed client key or password.
"""
from __future__ import annotations

from common.auth import AuthClaims, Role
from common.errors import NotFoundError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from api.admin.repository import (
    create_tenant_for_own_account,
    create_tenant_with_admin,
    delete_own_tenant,
    list_tenants_for_own_account,
    rotate_client_key,
)
from api.auth.dependencies import require_roles
from api.auth.repository import get_tenant_for_switch
from api.auth.tokens import create_access_token
from api.config import get_api_settings

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/tenants", tags=["admin"])

_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"
# Simple, dependency-free email shape check (this codebase does not install
# pydantic's optional "email" extra / email-validator -- api.auth.routes
# validates email fields as plain str too). Not a full RFC 5322 validator,
# just a sanity gate on the shape.
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AdminOnboardTenantRequest(BaseModel):
    """Body for POST /admin/tenants."""

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=63, pattern=_SLUG_PATTERN)
    admin_email: str = Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)
    admin_password: str | None = Field(default=None, min_length=12)
    admin_name: str | None = None


class AdminOnboardTenantResponse(BaseModel):
    """Response for POST /admin/tenants.

    ``admin_password`` is present ONLY when the server generated it (the
    caller omitted ``admin_password``) -- when the caller supplies their own
    password it is never echoed back.
    """

    tenant_id: str
    name: str
    slug: str
    client_key: str
    admin_user_id: str
    admin_email: str
    admin_password: str | None = None


class AdminRotateKeyResponse(BaseModel):
    """Response for POST /admin/tenants/{tenant_id}/rotate-key."""

    tenant_id: str
    client_key: str


@router.post("", status_code=status.HTTP_201_CREATED)
async def onboard_tenant(
    body: AdminOnboardTenantRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.PLATFORM_ADMIN)),  # noqa: B008
) -> AdminOnboardTenantResponse:
    """Create a tenant + hashed client key + first CLIENT_ADMIN user in one call.

    422 ``TENANT_SLUG_TAKEN`` on a duplicate slug (nothing else inserted);
    422 ``ADMIN_EMAIL_TAKEN`` on a duplicate admin email (the tenant row and
    its hashed client key already exist at this point -- decision 2, no
    auto-rollback).
    """
    db = request.app.state.db

    result = await create_tenant_with_admin(
        db,
        claims,
        name=body.name,
        slug=body.slug,
        admin_email=body.admin_email,
        admin_password=body.admin_password,
        admin_name=body.admin_name,
    )

    _log.info(
        "tenant onboarded",
        extra={
            "event": "tenant_onboarded",
            "tenant_id": result["tenant_id"],
            "slug": result["slug"],
            "admin_email": result["admin_email"],
        },
    )

    return AdminOnboardTenantResponse(
        tenant_id=result["tenant_id"],
        name=result["name"],
        slug=result["slug"],
        client_key=result["client_key"],
        admin_user_id=result["admin_user_id"],
        admin_email=result["admin_email"],
        admin_password=result["admin_password"] if result["password_was_generated"] else None,
    )


class CreateOwnTenantRequest(BaseModel):
    """Body for POST /admin/tenants/mine (CLIENT_ADMIN self-service)."""

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=63, pattern=_SLUG_PATTERN)


class CreateOwnTenantResponse(BaseModel):
    """Response for POST /admin/tenants/mine."""

    tenant_id: str
    name: str
    slug: str
    client_key: str


class OwnTenantSummary(BaseModel):
    """One row in GET /admin/tenants/mine."""

    id: str
    name: str
    slug: str
    enabled: bool


class ListOwnTenantsResponse(BaseModel):
    """Response for GET /admin/tenants/mine."""

    tenants: list[OwnTenantSummary]


@router.post("/mine", status_code=status.HTTP_201_CREATED)
async def create_own_tenant(
    body: CreateOwnTenantRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> CreateOwnTenantResponse:
    """Self-service: create a new chatbot under the CALLER'S OWN account
    (multi-chatbot accounts). Creates no new user -- every existing member
    of the account already reaches the new chatbot via
    ``POST /auth/switch-tenant``.

    422 ``TENANT_SLUG_TAKEN`` on a duplicate slug. No cap on how many
    chatbots one account may create (accepted gap -- no billing/plan-tier
    system exists yet).
    """
    db = request.app.state.db

    result = await create_tenant_for_own_account(db, claims, name=body.name, slug=body.slug)

    _log.info(
        "self-service chatbot created",
        extra={
            "event": "own_tenant_created",
            "tenant_id": result["tenant_id"],
            "slug": result["slug"],
        },
    )

    return CreateOwnTenantResponse(
        tenant_id=result["tenant_id"],
        name=result["name"],
        slug=result["slug"],
        client_key=result["client_key"],
    )


@router.get("/mine")
async def list_own_tenants(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ListOwnTenantsResponse:
    """Self-service: list every chatbot in the CALLER'S OWN account -- the
    data behind the "my chatbots" hub/switcher. Both CLIENT_ADMIN and
    CLIENT_AGENT get the same visibility here (the switcher is symmetric)."""
    db = request.app.state.db

    rows = await list_tenants_for_own_account(db, claims)
    return ListOwnTenantsResponse(
        tenants=[
            OwnTenantSummary(id=r["id"], name=r["name"], slug=r["slug"], enabled=r["enabled"])
            for r in rows
        ]
    )


class DeleteOwnTenantResponse(BaseModel):
    """Response for DELETE /admin/tenants/mine.

    ``tenant_id``/``name``/``slug`` describe the chatbot the session was
    re-minted onto (never the deleted one) -- same shape as
    ``SwitchTenantResponse`` so admin-web can update its UI the same way it
    does after an explicit switch.
    """

    deleted_tenant_id: str
    tenant_id: str
    name: str
    slug: str


@router.delete("/mine")
async def delete_own_chatbot(
    request: Request,
    response: Response,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> DeleteOwnTenantResponse:
    """Permanently delete the CALLER'S OWN currently-active chatbot, then
    re-mint the session cookie onto another enabled chatbot in the same
    account in the SAME response -- exactly like ``POST /auth/switch-tenant``,
    so the client never has to make a follow-up switch call.

    422 ``LAST_CHATBOT`` if this is the account's only remaining enabled
    chatbot. See ``api.admin.repository.delete_own_tenant`` for the full
    delete transaction (18 dependent tables cleared explicitly, then the
    tenant row itself, which cascades the rest).
    """
    settings = get_api_settings()
    db = request.app.state.db

    result = await delete_own_tenant(db, claims)
    next_tenant_id = result["next_tenant_id"]

    target = await get_tenant_for_switch(db, next_tenant_id)
    if target is None:
        # The guard inside delete_own_tenant only lets this happen if the
        # resolved next tenant vanished between the delete transaction
        # committing and this lookup -- not reachable in practice (nothing
        # else deletes tenants), but fail loudly rather than crash on
        # target["id"] below.
        raise NotFoundError("Tenant not found.", code="TENANT_NOT_FOUND")

    new_claims = AuthClaims(
        subject=claims.subject,
        role=claims.role,
        tenant_id=str(target["id"]),
    )

    ttl = settings.access_token_ttl_seconds
    token, _jti = create_access_token(new_claims, secret=settings.jwt_secret, ttl_seconds=ttl)

    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=ttl,
        path="/",
    )

    _log.info(
        "chatbot deleted",
        extra={"event": "own_tenant_deleted", "tenant_id": result["deleted_tenant_id"]},
    )

    try:
        from api.audit.repository import record_audit

        await record_audit(
            db,
            new_claims,
            action="admin.delete_own_tenant",
            target_type="tenant",
            target_id=result["deleted_tenant_id"],
        )
    except Exception:
        _log.warning(
            "failed to record audit event for delete_own_tenant",
            extra={"event": "audit_record_failed"},
        )

    return DeleteOwnTenantResponse(
        deleted_tenant_id=result["deleted_tenant_id"],
        tenant_id=str(target["id"]),
        name=str(target["name"]),
        slug=str(target["slug"]),
    )


@router.post("/{tenant_id}/rotate-key")
async def rotate_key(
    tenant_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.PLATFORM_ADMIN)),  # noqa: B008
) -> AdminRotateKeyResponse:
    """Mint a fresh client key for ``tenant_id``, invalidating the old one immediately.

    404 ``TENANT_NOT_FOUND`` for an unknown ``tenant_id``.
    """
    db = request.app.state.db

    new_key = await rotate_client_key(db, claims, tenant_id)
    if new_key is None:
        raise NotFoundError("Tenant not found.", code="TENANT_NOT_FOUND")

    _log.info(
        "client key rotated",
        extra={"event": "client_key_rotated", "tenant_id": tenant_id},
    )

    return AdminRotateKeyResponse(tenant_id=tenant_id, client_key=new_key)
