"""Admin/agent account routes -- POST/GET /admin/accounts, GET /admin/accounts/{id}.

RBAC per SR-9.2 D8: ``CLIENT_ADMIN`` full access; ``CLIENT_AGENT`` read-only
(403 on POST); ``VISITOR`` rejected on everything; ``PLATFORM_ADMIN`` rejected
on the implicit routes, allowed only via the tenant-explicit
``/admin/tenants/{tenant_id}/accounts`` family (D9), mirroring
``leads/admin_routes.py`` exactly.

Every access is tenant-scoped via ``claims.tenant_id`` -- a cross-tenant
``account_id`` is indistinguishable from a missing one and returns 404 (no
existence leak). No DELETE endpoint this increment (D8).
"""
from __future__ import annotations

from datetime import datetime

from common.auth import AuthClaims, Role
from common.errors import NotFoundError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from api.accounts.repository import Account, create_account, get_account, list_accounts
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/accounts", tags=["accounts"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/accounts", tags=["accounts"])


class AccountCreateRequest(BaseModel):
    """Body for POST /admin/accounts."""

    name: str = Field(min_length=1)
    domain: str | None = None

    @field_validator("name")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class AccountResponse(BaseModel):
    """Leak-free (no ``tenant_id``) account for the admin surface."""

    account_id: str
    name: str
    domain: str | None
    created_at: datetime


def _to_response(account: Account) -> AccountResponse:
    return AccountResponse(
        account_id=account.account_id,
        name=account.name,
        domain=account.domain,
        created_at=account.created_at,
    )


class AccountListResponse(BaseModel):
    """Paginated envelope for ``GET /admin/accounts``."""

    items: list[AccountResponse]
    total: int
    limit: int
    offset: int


async def _post_account(
    body: AccountCreateRequest, request: Request, claims: AuthClaims,
) -> AccountResponse:
    """Create an account. Audited; PII-free logging (account name is business
    data, not personal data, but is still excluded from the log line)."""
    db = request.app.state.db

    account_id = await create_account(db, claims, name=body.name, domain=body.domain)

    await record_audit(
        db,
        claims,
        action="account_created",
        target_type="account",
        target_id=account_id,
        metadata={},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "account created",
        extra={"event": "account_created", "account_id": account_id, "tenant_id": claims.tenant_id},
    )

    account = await get_account(db, claims, account_id)
    if account is None:
        raise NotFoundError("Account not found immediately after creation.", code="NOT_FOUND")
    return _to_response(account)


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_account(
    body: AccountCreateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AccountResponse:
    return await _post_account(body, request, claims)


@tenant_scoped_router.post("", status_code=status.HTTP_201_CREATED)
async def post_account_for_tenant(
    body: AccountCreateRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AccountResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/accounts``."""
    return await _post_account(body, request, claims)


async def _list_accounts(
    request: Request, claims: AuthClaims, *, limit: int, offset: int,
) -> AccountListResponse:
    db = request.app.state.db

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)

    accounts, total = await list_accounts(db, claims, limit=clamped_limit, offset=clamped_offset)

    _log.info(
        "accounts listed",
        extra={
            "event": "accounts_listed",
            "tenant_id": claims.tenant_id,
            "result_count": len(accounts),
        },
    )

    return AccountListResponse(
        items=[_to_response(a) for a in accounts],
        total=total,
        limit=clamped_limit,
        offset=clamped_offset,
    )


@router.get("")
async def list_accounts_route(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AccountListResponse:
    return await _list_accounts(request, claims, limit=limit, offset=offset)


@tenant_scoped_router.get("")
async def list_accounts_route_for_tenant(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AccountListResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/accounts``."""
    return await _list_accounts(request, claims, limit=limit, offset=offset)


async def _get_account_detail(
    account_id: str, request: Request, claims: AuthClaims,
) -> AccountResponse:
    """Fetch an account's detail. Returns 404 if missing or cross-tenant."""
    db = request.app.state.db

    account = await get_account(db, claims, account_id)
    if account is None:
        raise NotFoundError("Account not found.", code="NOT_FOUND")

    return _to_response(account)


@router.get("/{account_id}")
async def get_account_detail(
    account_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AccountResponse:
    return await _get_account_detail(account_id, request, claims)


@tenant_scoped_router.get("/{account_id}")
async def get_account_detail_for_tenant(
    account_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AccountResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/accounts/{account_id}``."""
    return await _get_account_detail(account_id, request, claims)
