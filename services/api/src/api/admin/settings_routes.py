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

from typing import Any, Literal

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from api.admin.settings_repository import BotSettings, get_bot_settings, upsert_bot_settings
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.orchestrator.config_repository import get_orchestrator_config, upsert_orchestrator_config

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
    bot_name: str | None = Field(default=None, max_length=40)
    accent_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    launcher_position: Literal["left", "right"] | None = None
    suggested_questions: list[str] | None = Field(default=None, max_length=5)
    # Tier 2: the turn-count cap is now settable from this same screen.
    # `None` (the default, e.g. an admin-web submission that omits it) means
    # "leave it as-is" -- `_put_settings` below only touches
    # `tenant_orchestrator_configs` when this is explicitly provided, so a
    # qualitative-only save (the pre-existing behavior) never clobbers it.
    turn_cap: int | None = Field(default=None, ge=1)
    # Same "None means leave as-is" contract as turn_cap, for the repeated-
    # low-confidence early-escalate streak length.
    low_confidence_streak_cap: int | None = Field(default=None, ge=1)

    @field_validator("sidebar_workspace_label", "dashboard_title", "bot_name")
    @classmethod
    def _empty_workspace_labels_are_null(cls, value: str | None) -> str | None:
        """Preserve the UI's nullable-label fallback contract on direct API use too."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("suggested_questions")
    @classmethod
    def _validate_suggested_questions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Suggested questions cannot be blank.")
        if any(len(item) > 200 for item in cleaned):
            raise ValueError("Each suggested question must be 200 characters or fewer.")
        return cleaned


class AdminBotSettingsResponse(BaseModel):
    """Unified read: qualitative fields + EXISTING thresholds + provider/model."""

    greeting: str | None
    business_hours: dict[str, Any] | None
    escalation_policy: str | None
    tone: str | None
    launcher_label: str | None
    sidebar_workspace_label: str | None
    dashboard_title: str | None
    bot_name: str | None
    accent_color: str | None
    launcher_position: str | None
    suggested_questions: list[str] | None
    answer_threshold: float
    escalate_threshold: float
    turn_cap: int
    low_confidence_streak_cap: int
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
        bot_name=settings.bot_name,
        accent_color=settings.accent_color,
        launcher_position=settings.launcher_position,
        suggested_questions=settings.suggested_questions,
        answer_threshold=settings.answer_threshold,
        escalate_threshold=settings.escalate_threshold,
        turn_cap=settings.turn_cap,
        low_confidence_streak_cap=settings.low_confidence_streak_cap,
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
    """Write the qualitative fields, plus ``turn_cap``/``low_confidence_streak_cap``
    when provided.

    Thresholds/``identity_gate_enabled``/provider/model stay untouched here
    otherwise. Both fields live in ``tenant_orchestrator_configs``, a
    DIFFERENT table from ``tenant_bot_settings`` -- when the caller provides
    either one, the CURRENT orchestrator config is read first so
    ``upsert_orchestrator_config`` (a full-row upsert) can be called with the
    tenant's existing ``answer_threshold``/``escalate_threshold``/
    ``identity_gate_enabled``/(the other of the two caps) alongside the new
    value, instead of silently clobbering the rest back to their defaults.
    ``None`` for either field (its default) means "not provided" -- if
    NEITHER is provided, the orchestrator table is left completely
    untouched, exactly as before this field existed.
    """
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
        bot_name=body.bot_name,
        accent_color=body.accent_color,
        launcher_position=body.launcher_position,
        suggested_questions=body.suggested_questions,
    )

    if body.turn_cap is not None or body.low_confidence_streak_cap is not None:
        current_orchestrator_config = await get_orchestrator_config(db, claims)
        await upsert_orchestrator_config(
            db,
            claims,
            answer_threshold=current_orchestrator_config.answer_threshold,
            escalate_threshold=current_orchestrator_config.escalate_threshold,
            turn_cap=(
                body.turn_cap if body.turn_cap is not None else current_orchestrator_config.turn_cap
            ),
            identity_gate_enabled=current_orchestrator_config.identity_gate_enabled,
            low_confidence_streak_cap=(
                body.low_confidence_streak_cap
                if body.low_confidence_streak_cap is not None
                else current_orchestrator_config.low_confidence_streak_cap
            ),
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
