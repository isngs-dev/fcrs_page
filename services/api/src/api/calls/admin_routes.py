"""Admin routes for missed-call text-back config -- CLIENT_ADMIN only.

Matches ``ingestion``/``training``'s admin-only RBAC convention (phone number
+ messaging setup is configuration, not review) rather than ``leads``'
agent-inclusive one. Dual-router pattern (implicit ``require_roles`` +
``/admin/tenants/{tenant_id}/...`` ``resolve_tenant_scope`` twin for
PLATFORM_ADMIN) mirrors every other admin module in this codebase, e.g.
``api.training.routes``/``api.admin.settings_routes``.

No secrets in this response -- the phone number and text-back message are
not sensitive, so (unlike calendar/notification config) nothing is withheld
from the echo.
"""
from __future__ import annotations

from common.auth import AuthClaims, Role
from common.errors import ValidationError
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, field_validator

from api.auth.dependencies import require_roles, resolve_tenant_scope
from api.calls.repository import get_call_config, upsert_call_config
from api.notifications.providers import validate_recipient_for_channel

router = APIRouter(prefix="/admin/calls", tags=["calls"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/calls", tags=["calls"])


class CallConfigRequest(BaseModel):
    """Body for PUT /admin/calls/config."""

    monitored_phone_number: str
    enabled: bool = False
    text_back_message: str

    @field_validator("monitored_phone_number")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        try:
            validate_recipient_for_channel("sms", v)
        except ValidationError as exc:
            # Re-raised as a plain ValueError so Pydantic's own 422 shape is
            # used here, consistent with text_back_message's blank check
            # below -- validate_recipient_for_channel's ValidationError is
            # meant for direct route bodies, not a field_validator.
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("text_back_message")
    @classmethod
    def _validate_message(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text_back_message must not be blank")
        return v


class CallConfigResponse(BaseModel):
    """Leak-free (no tenant_id) response -- nothing here is secret."""

    monitored_phone_number: str | None
    enabled: bool
    text_back_message: str | None


async def _get_config(request: Request, claims: AuthClaims) -> CallConfigResponse:
    db = request.app.state.db
    config = await get_call_config(db, claims)
    if config is None:
        return CallConfigResponse(
            monitored_phone_number=None, enabled=False, text_back_message=None,
        )
    return CallConfigResponse(
        monitored_phone_number=config.monitored_phone_number,
        enabled=config.enabled,
        text_back_message=config.text_back_message,
    )


async def _put_config(
    request: Request, claims: AuthClaims, body: CallConfigRequest
) -> CallConfigResponse:
    db = request.app.state.db
    await upsert_call_config(
        db,
        claims,
        monitored_phone_number=body.monitored_phone_number,
        enabled=body.enabled,
        text_back_message=body.text_back_message,
    )
    return CallConfigResponse(
        monitored_phone_number=body.monitored_phone_number,
        enabled=body.enabled,
        text_back_message=body.text_back_message,
    )


@router.get("/config")
async def get_config(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> CallConfigResponse:
    return await _get_config(request, claims)


@router.put("/config")
async def put_config(
    body: CallConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> CallConfigResponse:
    return await _put_config(request, claims, body)


@tenant_scoped_router.get("/config")
async def get_config_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> CallConfigResponse:
    """PLATFORM_ADMIN super-user variant of GET /admin/calls/config."""
    return await _get_config(request, claims)


@tenant_scoped_router.put("/config")
async def put_config_for_tenant(
    body: CallConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> CallConfigResponse:
    """PLATFORM_ADMIN super-user variant of PUT /admin/calls/config."""
    return await _put_config(request, claims, body)
