"""Admin/agent contact routes -- POST/GET /admin/contacts, GET/PATCH
/admin/contacts/{id}.

RBAC per SR-9.2 D8: ``CLIENT_ADMIN`` full access; ``CLIENT_AGENT`` read-only
(403 on POST/PATCH); ``VISITOR`` rejected on everything; ``PLATFORM_ADMIN``
rejected on the implicit routes, allowed only via the tenant-explicit
``/admin/tenants/{tenant_id}/contacts`` family (D9), mirroring
``leads/admin_routes.py`` exactly.

Every access is tenant-scoped via ``claims.tenant_id`` -- a cross-tenant
``contact_id`` is indistinguishable from a missing one and returns 404 (no
existence leak). No DELETE endpoint this increment (D8). No public/visitor
endpoint at all -- Contacts are never reachable from the widget.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, field_validator

from api.accounts.repository import get_account
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.contacts.repository import (
    Contact,
    create_contact,
    get_contact,
    list_contacts,
    update_contact,
)

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/contacts", tags=["contacts"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/contacts", tags=["contacts"])


class ConsentPayload(BaseModel):
    """Consent metadata provided by the admin creating a manual contact."""

    granted: bool
    purpose: str
    text: str


class ContactCreateRequest(BaseModel):
    """Body for POST /admin/contacts (D2: manual creation, no originating Lead)."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    account_id: str | None = None
    consent: ConsentPayload | None = None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str | None) -> str | None:
        if v is not None and "@" not in v:
            raise ValueError("email must contain @")
        return v


class ContactUpdateRequest(BaseModel):
    """Body for PATCH /admin/contacts/{id}. Only supplied fields change.

    Uses ``exclude_unset`` on the caller side (see ``_patch_contact``) so a
    field omitted from the JSON body is left untouched, while an explicit
    ``null`` (e.g. ``account_id: null``) clears/unlinks it.
    """

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    account_id: str | None = None


class ContactResponse(BaseModel):
    """Leak-free (no ``tenant_id``, no raw consent text) contact for the admin surface."""

    contact_id: str
    account_id: str | None
    lead_id: str | None
    name: str | None
    email: str | None
    phone: str | None
    owner_agent_id: str | None
    created_at: datetime


def _to_response(contact: Contact) -> ContactResponse:
    return ContactResponse(
        contact_id=contact.contact_id,
        account_id=contact.account_id,
        lead_id=contact.lead_id,
        name=contact.name,
        email=contact.email,
        phone=contact.phone,
        owner_agent_id=contact.owner_agent_id,
        created_at=contact.created_at,
    )


class ContactListResponse(BaseModel):
    """Paginated envelope for ``GET /admin/contacts``."""

    items: list[ContactResponse]
    total: int
    limit: int
    offset: int


async def _validate_account_id(
    db: Any, claims: AuthClaims, account_id: str | None,
) -> None:
    """Raise 422 ``INVALID_ACCOUNT`` if ``account_id`` is supplied but does not
    belong to the caller's tenant (D3/constraints: no silent-NULL fallback)."""
    if account_id is None:
        return
    account = await get_account(db, claims, account_id)
    if account is None:
        raise ValidationError(
            "The specified account does not exist in this tenant.",
            code="INVALID_ACCOUNT",
        )


async def _post_contact(
    body: ContactCreateRequest, request: Request, claims: AuthClaims,
) -> ContactResponse:
    """Create a Contact manually, with no originating Lead (D2).

    Consent gate (D7): the body must carry a consent object with
    ``granted=true`` or the call fails 422 ``CONSENT_REQUIRED`` and nothing
    is persisted. ``captured_at`` is server-stamped (the admin asserted
    consent at this moment; distinct from a conversion's carried-over
    original timestamp).
    """
    if body.consent is None or body.consent.granted is not True:
        raise ValidationError(
            "Consent to store contact information is required.",
            code="CONSENT_REQUIRED",
        )

    db = request.app.state.db

    await _validate_account_id(db, claims, body.account_id)

    consent_with_timestamp = {
        "granted": body.consent.granted,
        "purpose": body.consent.purpose,
        "text": body.consent.text,
        "captured_at": datetime.now(UTC).isoformat(),
        "basis": "admin_asserted",
    }

    contact_id = await create_contact(
        db,
        claims,
        account_id=body.account_id,
        lead_id=None,
        name=body.name,
        email=body.email,
        phone=body.phone,
        consent=consent_with_timestamp,
    )

    await record_audit(
        db,
        claims,
        action="contact_created",
        target_type="contact",
        target_id=contact_id,
        metadata={"account_id": body.account_id, "lead_id": None},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "contact created",
        extra={"event": "contact_created", "contact_id": contact_id, "tenant_id": claims.tenant_id},
    )

    contact = await get_contact(db, claims, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found immediately after creation.", code="NOT_FOUND")
    return _to_response(contact)


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_contact(
    body: ContactCreateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ContactResponse:
    return await _post_contact(body, request, claims)


@tenant_scoped_router.post("", status_code=status.HTTP_201_CREATED)
async def post_contact_for_tenant(
    body: ContactCreateRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ContactResponse:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/contacts``."""
    return await _post_contact(body, request, claims)


async def _list_contacts(
    request: Request, claims: AuthClaims, *, limit: int, offset: int,
) -> ContactListResponse:
    db = request.app.state.db

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)

    contacts, total = await list_contacts(db, claims, limit=clamped_limit, offset=clamped_offset)

    _log.info(
        "contacts listed",
        extra={
            "event": "contacts_listed",
            "tenant_id": claims.tenant_id,
            "result_count": len(contacts),
        },
    )

    return ContactListResponse(
        items=[_to_response(c) for c in contacts],
        total=total,
        limit=clamped_limit,
        offset=clamped_offset,
    )


@router.get("")
async def list_contacts_route(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ContactListResponse:
    return await _list_contacts(request, claims, limit=limit, offset=offset)


@tenant_scoped_router.get("")
async def list_contacts_route_for_tenant(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ContactListResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/contacts``."""
    return await _list_contacts(request, claims, limit=limit, offset=offset)


async def _get_contact_detail(
    contact_id: str, request: Request, claims: AuthClaims,
) -> ContactResponse:
    """Fetch a contact's detail. Returns 404 if missing or cross-tenant."""
    db = request.app.state.db

    contact = await get_contact(db, claims, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found.", code="NOT_FOUND")

    return _to_response(contact)


@router.get("/{contact_id}")
async def get_contact_detail(
    contact_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ContactResponse:
    return await _get_contact_detail(contact_id, request, claims)


@tenant_scoped_router.get("/{contact_id}")
async def get_contact_detail_for_tenant(
    contact_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> ContactResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/contacts/{contact_id}``."""
    return await _get_contact_detail(contact_id, request, claims)


async def _patch_contact(
    contact_id: str, body: ContactUpdateRequest, request: Request, claims: AuthClaims,
) -> ContactResponse:
    """Partially update a contact. 404 if missing/cross-tenant.

    Only fields present in the JSON body are changed (``exclude_unset``);
    an explicit ``account_id: null`` unlinks the account. A supplied
    ``account_id`` from another tenant is rejected 422 ``INVALID_ACCOUNT``.
    """
    db = request.app.state.db

    existing = await get_contact(db, claims, contact_id)
    if existing is None:
        raise NotFoundError("Contact not found.", code="NOT_FOUND")

    fields = body.model_dump(exclude_unset=True)

    if "account_id" in fields:
        await _validate_account_id(db, claims, fields["account_id"])

    update_kwargs: dict[str, Any] = {}
    for key in ("name", "email", "phone", "account_id"):
        if key in fields:
            update_kwargs[key] = fields[key]

    if update_kwargs:
        updated = await update_contact(db, claims, contact_id, **update_kwargs)
        if updated is None:
            raise NotFoundError("Contact not found.", code="NOT_FOUND")
    else:
        updated = existing

    await record_audit(
        db,
        claims,
        action="contact_updated",
        target_type="contact",
        target_id=contact_id,
        metadata={"updated_fields": sorted(update_kwargs.keys())},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "contact updated",
        extra={
            "event": "contact_updated",
            "contact_id": contact_id,
            "tenant_id": claims.tenant_id,
            "updated_fields": sorted(update_kwargs.keys()),
        },
    )

    return _to_response(updated)


@router.patch("/{contact_id}")
async def patch_contact(
    contact_id: str,
    body: ContactUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ContactResponse:
    return await _patch_contact(contact_id, body, request, claims)


@tenant_scoped_router.patch("/{contact_id}")
async def patch_contact_for_tenant(
    contact_id: str,
    body: ContactUpdateRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> ContactResponse:
    """PLATFORM_ADMIN super-user variant of ``PATCH /admin/contacts/{contact_id}``."""
    return await _patch_contact(contact_id, body, request, claims)
