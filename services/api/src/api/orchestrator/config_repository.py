"""Per-tenant orchestrator config repository -- the 3-way decision thresholds.

Mirrors ``api.scheduling.calendar_config_repository`` / ``api.llm
.config_repository``'s tenant-scoping conventions, but the thresholds are
plain floats (no secret material, nothing to encrypt). Owned by the
orchestrator module (CLAUDE.md §4 "keep module seams strict") -- a distinct
table from ``tenant_llm_configs``, since thresholds are orchestration policy,
not LLM-provider config.
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.config import get_api_settings


@dataclass(frozen=True)
class OrchestratorConfig:
    """A tenant's 3-way decision thresholds + turn-count cap (S10.4) +
    identity-gate toggle (SR-14 D9).

    ``turn_cap`` and ``identity_gate_enabled`` are always resolved (never
    ``None``) -- see ``get_orchestrator_config``. ``identity_gate_enabled``
    defaults to ``False`` (OFF) for both an unconfigured tenant and a row
    with ``identity_gate_enabled IS NULL`` (an S10.2/S10.4-era row predating
    this column) -- no existing tenant's behavior changes silently.
    """

    answer_threshold: float
    escalate_threshold: float
    turn_cap: int = 6
    identity_gate_enabled: bool = False


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Orchestrator config is always tenant-scoped; a global caller has no
    tenant_id and therefore cannot be filtered to a tenant's row.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Orchestrator config repository is tenant-scoped; PLATFORM_ADMIN "
            "callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_orchestrator_config(db: Database, claims: AuthClaims) -> OrchestratorConfig:
    """Fetch the caller's tenant orchestrator config, or settings defaults.

    Never returns ``None`` -- an unconfigured tenant is deterministic via
    ``settings.orchestrator_default_answer_threshold`` /
    ``orchestrator_default_escalate_threshold``. Raises ``ValidationError``
    for global callers.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT answer_threshold, escalate_threshold, turn_cap, identity_gate_enabled "
        "FROM tenant_orchestrator_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    settings = get_api_settings()
    if row is None:
        return OrchestratorConfig(
            answer_threshold=settings.orchestrator_default_answer_threshold,
            escalate_threshold=settings.orchestrator_default_escalate_threshold,
            turn_cap=settings.orchestrator_default_turn_cap,
            identity_gate_enabled=False,
        )

    row_turn_cap = row["turn_cap"]
    turn_cap = (
        int(row_turn_cap) if row_turn_cap is not None else settings.orchestrator_default_turn_cap
    )
    row_identity_gate_enabled = row["identity_gate_enabled"]
    identity_gate_enabled = (
        bool(row_identity_gate_enabled) if row_identity_gate_enabled is not None else False
    )
    return OrchestratorConfig(
        answer_threshold=float(row["answer_threshold"]),
        escalate_threshold=float(row["escalate_threshold"]),
        turn_cap=turn_cap,
        identity_gate_enabled=identity_gate_enabled,
    )


async def upsert_orchestrator_config(
    db: Database,
    claims: AuthClaims,
    *,
    answer_threshold: float,
    escalate_threshold: float,
    turn_cap: int | None = None,
    identity_gate_enabled: bool | None = None,
) -> None:
    """Insert or update the caller's tenant orchestrator config.

    Raises ``ValidationError`` for global callers, ``ValidationError``
    (``INVALID_ORCHESTRATOR_THRESHOLDS``) if
    ``0 <= escalate_threshold <= answer_threshold <= 1`` does not hold
    (defense-in-depth over the DB CHECK constraint), and ``ValidationError``
    (``INVALID_TURN_CAP``) if ``turn_cap`` is not ``None`` and ``< 1``
    (defense-in-depth over the 0027 CHECK constraint). ``turn_cap`` and
    ``identity_gate_enabled`` are always bound (an explicit ``None`` clears
    each back to its default -- the settings turn_cap default, and ``False``
    for the identity gate, SR-14 D9).
    """
    _reject_global(claims)

    if not (0.0 <= escalate_threshold <= answer_threshold <= 1.0):
        raise ValidationError(
            "Thresholds must satisfy 0 <= escalate_threshold <= "
            "answer_threshold <= 1.",
            code="INVALID_ORCHESTRATOR_THRESHOLDS",
        )

    if turn_cap is not None and turn_cap < 1:
        raise ValidationError(
            "turn_cap must be >= 1.",
            code="INVALID_TURN_CAP",
        )

    await db.execute(
        "INSERT INTO tenant_orchestrator_configs "
        "(tenant_id, answer_threshold, escalate_threshold, turn_cap, identity_gate_enabled) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "answer_threshold = $2, escalate_threshold = $3, turn_cap = $4, "
        "identity_gate_enabled = $5, updated_at = now()",
        claims.tenant_id,
        answer_threshold,
        escalate_threshold,
        turn_cap,
        identity_gate_enabled,
    )
