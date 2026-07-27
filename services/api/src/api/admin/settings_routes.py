"""Tenant bot-settings routes -- GET/PUT /admin/settings (S12.2).

``GET`` is readable by ``CLIENT_ADMIN`` and ``CLIENT_AGENT`` (a read-only
view of the bot's config is reasonable for an agent reviewing conversations,
mirrors S11.2's RBAC choice). ``PUT`` is ``CLIENT_ADMIN``-only (CLAUDE.md:
``CLIENT_AGENT`` "cannot change config") and writes ONLY the four qualitative
columns -- thresholds/provider/model are read-only here, unchanged by this
endpoint (decision 6).

S12.7: ``/admin/tenants/{tenant_id}/settings`` mounts the SAME business logic
(``_get_settings``/``_put_settings``) for a PLATFORM_ADMIN super-user, via
``resolve_tenant_scope``. The implicit ``/admin/settings`` routes below are
byte-for-byte unchanged for CLIENT_ADMIN/CLIENT_AGENT.
"""
from __future__ import annotations

from typing import Any

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from api.admin.settings_repository import BotSettings, get_bot_settings, upsert_bot_settings
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/settings", tags=["admin"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/settings", tags=["admin"])


class AdminBotSettingsRequest(BaseModel):
    """Body for PUT /admin/settings -- the qualitative fields only."""

    greeting: str | None = Field(default=None, max_length=2000)
    business_hours: dict[str, Any] | None = None
    escalation_policy: str | None = Field(default=None, max_length=2000)
    tone: str | None = Field(default=None, max_length=100)
    launcher_label: str | None = Field(default=None, max_length=40)
    sidebar_workspace_label: str | None = Field(default=None, max_length=80)
    dashboard_title: str | None = Field(default=None, max_length=80)

    @field_validator("sidebar_workspace_label", "dashboard_title")
    @classmethod
    def _empty_workspace_labels_are_null(cls, value: str | None) -> str | None:
        """Preserve the UI's nullable-label fallback contract on direct API use too."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class AdminBotSettingsResponse(BaseModel):
    """Unified read: qualitative fields + EXISTING thresholds + provider/model."""

    greeting: str | None
    business_hours: dict[str, Any] | None
    escalation_policy: str | None
    tone: str | None
    launcher_label: str | None
    sidebar_workspace_label: str | None
    dashboard_title: str | None
    answer_threshold: float
    escalate_threshold: float
    turn_cap: int
    llm_provider: str | None
    llm_model: str | None


def _to_response(settings: BotSettings) -> AdminBotSettingsResponse:
    return AdminBotSettingsResponse(
        greeting=settings.greeting,
        business_hours=settings.business_hours,
        escalation_policy=settings.escalation_policy,
        tone=settings.tone,
        launcher_label=settings.launcher_label,
        sidebar_workspace_label=settings.sidebar_workspace_label,
        dashboard_title=settings.dashboard_title,
        answer_threshold=settings.answer_threshold,
        escalate_threshold=settings.escalate_threshold,
        turn_cap=settings.turn_cap,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
    )


async def _get_settings(request: Request, claims: AuthClaims) -> AdminBotSettingsResponse:
    """Unified read: qualitative bot config + existing thresholds + provider/model."""
    db = request.app.state.db

    settings = await get_bot_settings(db, claims)
    return _to_response(settings)


async def _put_settings(
    body: AdminBotSettingsRequest, request: Request, claims: AuthClaims,
) -> AdminBotSettingsResponse:
    """Write ONLY qualitative fields; thresholds/provider/model are untouched."""
    db = request.app.state.db

    await upsert_bot_settings(
        db,
        claims,
        greeting=body.greeting,
        business_hours=body.business_hours,
        escalation_policy=body.escalation_policy,
        tone=body.tone,
        launcher_label=body.launcher_label,
        sidebar_workspace_label=body.sidebar_workspace_label,
        dashboard_title=body.dashboard_title,
    )

    await record_audit(
        db,
        claims,
        action="tenant_bot_settings_updated",
        target_type="tenant_bot_settings",
        target_id=claims.tenant_id,
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "tenant bot settings updated",
        extra={"event": "tenant_bot_settings_updated", "tenant_id": claims.tenant_id},
    )

    settings = await get_bot_settings(db, claims)
    return _to_response(settings)


@router.get("")
async def get_settings(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AdminBotSettingsResponse:
    return await _get_settings(request, claims)


@router.put("")
async def put_settings(
    body: AdminBotSettingsRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AdminBotSettingsResponse:
    return await _put_settings(body, request, claims)


@tenant_scoped_router.get("")
async def get_settings_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AdminBotSettingsResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/settings`` (S12.7)."""
    return await _get_settings(request, claims)


@tenant_scoped_router.put("")
async def put_settings_for_tenant(
    body: AdminBotSettingsRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AdminBotSettingsResponse:
    """PLATFORM_ADMIN super-user variant of ``PUT /admin/settings`` (S12.7)."""
    return await _put_settings(body, request, claims)
