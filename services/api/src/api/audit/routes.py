"""Admin audit routes — read the audit trail.

GET /admin/audit returns a paginated list of audit events for the caller's
tenant.  CLIENT_ADMIN only; CLIENT_AGENT/VISITOR → 403.
"""
from __future__ import annotations

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request

from api.audit.chain import verify_audit_chain
from api.audit.repository import list_audit
from api.auth.dependencies import require_roles, resolve_tenant_scope

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/audit", tags=["audit"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/audit", tags=["audit"])


def _iso(dt: object) -> str | None:
    """Return ISO format string for a datetime, or pass through strings."""
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return str(dt.isoformat())
    return str(dt)


async def _list_audit_events(
    request: Request, claims: AuthClaims, *, limit: int, offset: int,
) -> list[dict[str, object]]:
    """List audit events for the caller's tenant, newest first.

    ``limit`` is clamped to ``[1, 200]``.
    Response excludes ``tenant_id``.
    """
    db = request.app.state.db

    events = await list_audit(db, claims, limit=limit, offset=offset)

    return [
        {
            "event_id": e.event_id,
            "actor": e.actor,
            "action": e.action,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "metadata": e.metadata,
            "created_at": _iso(e.created_at),
        }
        for e in events
    ]


@router.get("")
async def list_audit_events(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> list[dict[str, object]]:
    return await _list_audit_events(request, claims, limit=limit, offset=offset)


@tenant_scoped_router.get("")
async def list_audit_events_for_tenant(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> list[dict[str, object]]:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/audit`` (S12.7)."""
    return await _list_audit_events(request, claims, limit=limit, offset=offset)


async def _verify(request: Request, claims: AuthClaims) -> dict[str, object]:
    report = await verify_audit_chain(request.app.state.db, claims)
    return {
        "ok": report.ok,
        "rows_checked": report.rows_checked,
        "unsealed": report.unsealed,
        "broken_at": report.broken_at,
        "first_chained_at": _iso(report.first_chained_at),
    }


@router.get("/verify")
async def verify_chain(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, object]:
    """Recompute this tenant's audit hash chain (A1.5)."""
    return await _verify(request, claims)


@tenant_scoped_router.get("/verify")
async def verify_chain_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, object]:
    return await _verify(request, claims)
