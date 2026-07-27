"""Widget admission routes.

POST /widget/session -- validates the public client key + Origin allowlist,
mints a short-lived VISITOR JWT, returns it as a bearer token in the body.

GET /widget/whoami -- proves downstream VISITOR resolution from the bearer token.
"""
from __future__ import annotations

from common.auth import AuthClaims
from common.errors import AuthorizationError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.config import get_api_settings
from api.gateway.dependencies import get_visitor_claims
from api.gateway.repository import get_launcher_label, get_resume_enabled, get_tenant_by_client_key
from api.gateway.sessions import mint_visitor_session, origin_allowed
from api.ratelimit import client_ip, enforce_rate_limit

_log = get_logger(__name__)

router = APIRouter(prefix="/widget", tags=["widget"])


class WidgetSessionRequest(BaseModel):
    """Body for POST /widget/session."""

    client_key: str


@router.post("/session")
async def widget_session(
    body: WidgetSessionRequest,
    request: Request,
) -> dict[str, str | bool]:
    """Validate client key + Origin, mint a visitor session token."""
    settings = get_api_settings()

    # Rate limit by IP and by client_key (both must pass).
    ip = client_ip(request)
    await enforce_rate_limit(
        request,
        scope="widget_session_ip",
        identifier=ip,
        limit=settings.widget_session_rate_limit_max,
        window_seconds=settings.widget_session_rate_limit_window_seconds,
    )
    await enforce_rate_limit(
        request,
        scope="widget_session_key",
        identifier=body.client_key,
        limit=settings.widget_session_rate_limit_max,
        window_seconds=settings.widget_session_rate_limit_window_seconds,
    )

    db = request.app.state.db

    tenant = await get_tenant_by_client_key(db, body.client_key)
    if tenant is None:
        raise ValidationError("Unknown client key.", code="INVALID_CLIENT_KEY")

    if not tenant.get("enabled", True):
        raise AuthorizationError("Tenant is disabled.", code="TENANT_DISABLED")

    origin = request.headers.get("origin")
    allowed_origins: list[str] = tenant.get("allowed_origins", [])
    if not origin_allowed(origin, allowed_origins):
        raise AuthorizationError("Origin not allowed.", code="ORIGIN_NOT_ALLOWED")

    token, expires_at = mint_visitor_session(
        tenant["id"],
        secret=settings.jwt_secret,
        ttl_seconds=settings.visitor_session_ttl_seconds,
    )

    # SR-3 decision 8: a single read-only flag, echoed in the admission
    # response. Defaults false (opt-in) -- never changes mint_visitor_session,
    # the JWT, or any authorization SQL.
    resume_enabled = await get_resume_enabled(db, tenant["id"])
    launcher_label = await get_launcher_label(db, tenant["id"])

    _log.info(
        "visitor session minted",
        extra={"event": "widget_session", "tenant_id": tenant["id"]},
    )
    response: dict[str, str | bool] = {
        "visitor_token": token,
        "expires_at": expires_at,
        "resume_enabled": resume_enabled,
    }
    if launcher_label is not None:
        response["launcher_label"] = launcher_label
    return response


@router.get("/whoami")
async def widget_whoami(
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> dict[str, str | None]:
    """Return the visitor's identity from the bearer token."""
    return {
        "visitor_id": claims.subject,
        "tenant_id": claims.tenant_id,
        "role": claims.role.value,
    }
