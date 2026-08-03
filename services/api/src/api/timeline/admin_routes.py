"""Admin/agent customer-360 timeline routes (SR-9.3 D1/D7).

``GET /admin/contacts/{contact_id}/timeline`` and
``GET /admin/leads/{lead_id}/timeline`` -- each with its
``/admin/tenants/{tenant_id}/...`` tenant-explicit twin (D7, mirroring
``leads/admin_routes.py``'s established pattern). Both endpoints delegate to
one shared ``_impl`` per D1's requirement of zero per-route branching in the
union/merge/degradation/pagination/caching logic -- the only difference
between the two endpoints is which ``identity.py`` resolver produces the
``IdentitySet``.

RBAC (D7): ``CLIENT_ADMIN``/``CLIENT_AGENT`` read both; ``VISITOR`` rejected
everywhere; ``PLATFORM_ADMIN`` only via the tenant-explicit family. No
POST/PATCH/DELETE anywhere -- read-only surface.

Caching (D8): cache-aside around the full ``_impl`` result, tenant-scoped key
via ``cache_key(claims, "timeline", ...)``, TTL from settings. A degraded
result (``degraded=True``) is NEVER cached.

PII-safe logging (Scope §9): only ``event``/``tenant_id``/``contact_id``/
``lead_id``/``source``/correlation id -- never message content, email, name,
phone, or notification subject/body.
"""
from __future__ import annotations

from datetime import datetime

from common.auth import AuthClaims, Role
from common.cache import cache_key
from common.errors import NotFoundError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request

from api.auth.dependencies import require_roles, resolve_tenant_scope
from api.config import get_api_settings
from api.leads.repository import get_lead
from api.timeline.identity import resolve_contact_identities, resolve_lead_identities
from api.timeline.models import (
    TimelineItemResponse,
    TimelineResponse,
    TimelineResult,
    TimelineSourceState,
    TimelineSubject,
)
from api.timeline.service import build_timeline

_log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["timeline"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}", tags=["timeline"])

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_LIMIT))


def _to_response(subject: TimelineSubject, result: TimelineResult) -> TimelineResponse:
    """Build the leak-free wire response from a ``TimelineResult``."""
    return TimelineResponse(
        subject=subject,
        degraded=result.degraded,
        sources={
            name: TimelineSourceState(
                state=outcome.state, count=outcome.count, truncated=outcome.truncated,
            )
            for name, outcome in result.sources.items()
        },
        items=[
            TimelineItemResponse(
                kind=item.kind, occurred_at=item.occurred_at, id=item.item_id, data=item.data,
            )
            for item in result.items
        ],
        next_before=result.next_before,
    )


async def _get_contact_timeline_impl(
    contact_id: str,
    request: Request,
    claims: AuthClaims,
    *,
    before: datetime | None,
    limit: int,
) -> TimelineResponse:
    """Shared impl for GET /admin/contacts/{contact_id}/timeline (D1/D7).

    Identity resolution failure/missing (D6): propagates as a hard error --
    a missing/cross-tenant contact raises 404 here, BEFORE any source is
    queried and BEFORE anything is cached.
    """
    db = request.app.state.db
    cache = request.app.state.cache
    settings = get_api_settings()
    clamped_limit = _clamp_limit(limit)

    # Identity resolution is NEVER cached and NEVER degraded (D6) -- a
    # missing/cross-tenant subject is always a fresh 404.
    identities = await resolve_contact_identities(db, claims, contact_id)
    if identities is None:
        raise NotFoundError("Contact not found.", code="NOT_FOUND")

    cursor = before.isoformat() if before is not None else "head"
    key = cache_key(claims, "timeline", "contact", contact_id, cursor, str(clamped_limit))

    cached = await cache.get(key)
    if cached is not None:
        return TimelineResponse.model_validate_json(cached)

    result = await build_timeline(
        db, claims, identities=identities, before=before, limit=clamped_limit,
    )

    subject = TimelineSubject(kind="contact", id=contact_id, converted_to_contact_id=None)
    response = _to_response(subject, result)

    if not result.degraded:
        await cache.set(key, response.model_dump_json(), settings.timeline_cache_ttl_seconds)

    _log.info(
        "contact timeline read",
        extra={"event": "contact_timeline_read"},
    )

    return response


async def _get_lead_timeline_impl(
    lead_id: str,
    request: Request,
    claims: AuthClaims,
    *,
    before: datetime | None,
    limit: int,
) -> TimelineResponse:
    """Shared impl for GET /admin/leads/{lead_id}/timeline (D1/D2/D7).

    An already-converted lead returns ITS OWN items plus
    ``converted_to_contact_id`` in the subject -- it does NOT silently widen
    to the contact's fuller timeline (D2). The admin must explicitly follow
    ``converted_to_contact_id`` to the contact route for that.
    """
    db = request.app.state.db
    cache = request.app.state.cache
    settings = get_api_settings()
    clamped_limit = _clamp_limit(limit)

    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    identities = await resolve_lead_identities(db, claims, lead_id)
    if identities is None:
        raise NotFoundError("Lead not found.", code="NOT_FOUND")

    cursor = before.isoformat() if before is not None else "head"
    key = cache_key(claims, "timeline", "lead", lead_id, cursor, str(clamped_limit))

    cached = await cache.get(key)
    if cached is not None:
        return TimelineResponse.model_validate_json(cached)

    result = await build_timeline(
        db, claims, identities=identities, before=before, limit=clamped_limit,
    )

    subject = TimelineSubject(
        kind="lead", id=lead_id, converted_to_contact_id=lead.converted_to_contact_id,
    )
    response = _to_response(subject, result)

    if not result.degraded:
        await cache.set(key, response.model_dump_json(), settings.timeline_cache_ttl_seconds)

    _log.info(
        "lead timeline read",
        extra={"event": "lead_timeline_read"},
    )

    return response


@router.get("/contacts/{contact_id}/timeline")
async def get_contact_timeline(
    contact_id: str,
    request: Request,
    before: datetime | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=_DEFAULT_LIMIT),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> TimelineResponse:
    return await _get_contact_timeline_impl(contact_id, request, claims, before=before, limit=limit)


@tenant_scoped_router.get("/contacts/{contact_id}/timeline")
async def get_contact_timeline_for_tenant(
    contact_id: str,
    request: Request,
    before: datetime | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=_DEFAULT_LIMIT),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> TimelineResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/contacts/{contact_id}/timeline``."""
    return await _get_contact_timeline_impl(contact_id, request, claims, before=before, limit=limit)


@router.get("/leads/{lead_id}/timeline")
async def get_lead_timeline(
    lead_id: str,
    request: Request,
    before: datetime | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=_DEFAULT_LIMIT),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> TimelineResponse:
    return await _get_lead_timeline_impl(lead_id, request, claims, before=before, limit=limit)


@tenant_scoped_router.get("/leads/{lead_id}/timeline")
async def get_lead_timeline_for_tenant(
    lead_id: str,
    request: Request,
    before: datetime | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=_DEFAULT_LIMIT),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> TimelineResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/leads/{lead_id}/timeline``."""
    return await _get_lead_timeline_impl(lead_id, request, claims, before=before, limit=limit)
