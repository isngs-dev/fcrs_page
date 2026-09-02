"""Tenant bot-settings repository (S12.2) -- the qualitative bot config
(greeting, business hours, escalation policy, tone), UNIFIED at read time
with the EXISTING numeric thresholds (``api.orchestrator.config_repository
.get_orchestrator_config``, S10.2) and provider/model
(``tenant_llm_configs``, S3.1) -- decision 5.

``get_bot_settings`` is a read-only aggregate: it calls the orchestrator
module's existing, unmodified ``get_orchestrator_config`` (the one sanctioned
cross-module read, mirroring S10.4's precedent) and runs a new, narrow
``SELECT provider, model FROM tenant_llm_configs`` here -- never the
``api_key_ciphertext`` column, never an import of ``llm/config_repository
.py``'s decrypt path.

``upsert_bot_settings`` writes ONLY the qualitative columns
(decision 6); it never touches ``tenant_orchestrator_configs`` or
``tenant_llm_configs`` -- those keep their existing S10.2/S3.1 write paths.

Both functions reject a PLATFORM_ADMIN/global caller (``ValidationError
GLOBAL_CALLER_NOT_PERMITTED``, mirrors ``config_repository._reject_global``)
-- bot settings are always tenant-scoped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.orchestrator.config_repository import get_orchestrator_config


@dataclass(frozen=True)
class BotSettings:
    """Unified GET /admin/settings shape (decision 5)."""

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


def _reject_global(claims: AuthClaims) -> None:
    if claims.tenant_id is None:
        raise ValidationError(
            "Bot settings are tenant-scoped; PLATFORM_ADMIN callers are not "
            "permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_bot_settings(db: Database, claims: AuthClaims) -> BotSettings:
    """Fetch the caller's tenant bot settings, merged with the existing
    orchestrator thresholds and LLM provider/model.

    A tenant with no ``tenant_bot_settings`` row yet gets genuinely empty
    (``None``) qualitative fields -- NOT a fabricated string -- while the
    threshold fields are still populated via ``get_orchestrator_config``'s
    own never-``None`` defaulting (decision 5).
    """
    _reject_global(claims)

    settings_row = await db.fetchrow(
        "SELECT greeting, business_hours, escalation_policy, tone, launcher_label, "
        "sidebar_workspace_label, dashboard_title, bot_name, accent_color, "
        "launcher_position, suggested_questions "
        "FROM tenant_bot_settings WHERE tenant_id = $1",
        claims.tenant_id,
    )

    orchestrator_config = await get_orchestrator_config(db, claims)

    llm_row = await db.fetchrow(
        "SELECT provider, model FROM tenant_llm_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )

    return BotSettings(
        greeting=settings_row["greeting"] if settings_row is not None else None,
        business_hours=settings_row["business_hours"] if settings_row is not None else None,
        escalation_policy=(
            settings_row["escalation_policy"] if settings_row is not None else None
        ),
        tone=settings_row["tone"] if settings_row is not None else None,
        launcher_label=(
            settings_row["launcher_label"] if settings_row is not None else None
        ),
        sidebar_workspace_label=(
            settings_row["sidebar_workspace_label"] if settings_row is not None else None
        ),
        dashboard_title=(
            settings_row["dashboard_title"] if settings_row is not None else None
        ),
        bot_name=settings_row["bot_name"] if settings_row is not None else None,
        accent_color=settings_row["accent_color"] if settings_row is not None else None,
        launcher_position=(
            settings_row["launcher_position"] if settings_row is not None else None
        ),
        suggested_questions=(
            settings_row["suggested_questions"] if settings_row is not None else None
        ),
        answer_threshold=orchestrator_config.answer_threshold,
        escalate_threshold=orchestrator_config.escalate_threshold,
        turn_cap=orchestrator_config.turn_cap,
        low_confidence_streak_cap=orchestrator_config.low_confidence_streak_cap,
        llm_provider=str(llm_row["provider"]) if llm_row is not None else None,
        llm_model=str(llm_row["model"]) if llm_row is not None else None,
    )


async def upsert_bot_settings(
    db: Database,
    claims: AuthClaims,
    *,
    greeting: str | None,
    business_hours: dict[str, Any] | None,
    escalation_policy: str | None,
    tone: str | None,
    launcher_label: str | None,
    sidebar_workspace_label: str | None,
    dashboard_title: str | None,
    bot_name: str | None,
    accent_color: str | None,
    launcher_position: str | None,
    suggested_questions: list[str] | None,
) -> None:
    """Insert or update ONLY the qualitative columns for the caller's tenant.

    Never binds/updates a threshold or provider/model param -- those keep
    their existing S10.2/S3.1 write paths untouched (decision 6).
    """
    _reject_global(claims)

    await db.execute(
        "INSERT INTO tenant_bot_settings "
        "(tenant_id, greeting, business_hours, escalation_policy, tone, launcher_label, "
        "sidebar_workspace_label, dashboard_title, bot_name, accent_color, "
        "launcher_position, suggested_questions) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "greeting = $2, business_hours = $3, escalation_policy = $4, tone = $5, "
        "launcher_label = $6, sidebar_workspace_label = $7, dashboard_title = $8, "
        "bot_name = $9, accent_color = $10, launcher_position = $11, "
        "suggested_questions = $12, updated_at = now()",
        claims.tenant_id,
        greeting,
        business_hours,
        escalation_policy,
        tone,
        launcher_label,
        sidebar_workspace_label,
        dashboard_title,
        bot_name,
        accent_color,
        launcher_position,
        suggested_questions,
    )
