"""Per-tenant opportunity config repository -- currency + win-probabilities.

Clones ``api.orchestrator.config_repository``'s tenant-scoping conventions
exactly (SR-9.4 D3/D10/M5): one config table carries BOTH currency and
per-stage win-probabilities, resolved by a never-``None``
``get_opportunity_config`` and written by ``upsert_opportunity_config``
with defense-in-depth Python validation over the DB CHECK constraints.
Owned by the opportunities module (CLAUDE.md §2 "keep module seams
strict") -- a distinct table from ``tenant_orchestrator_configs``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.config import get_api_settings

_CURRENCY_RE_LEN = 3
_NON_TERMINAL_STAGES: tuple[str, ...] = ("prospecting", "qualification", "proposal", "negotiation")
_TERMINAL_STAGES: frozenset[str] = frozenset({"closed_won", "closed_lost"})


@dataclass(frozen=True)
class OpportunityConfig:
    """A tenant's currency + per-stage win-probabilities.

    ``stage_probabilities`` only ever carries the four non-terminal stage
    keys -- terminal probabilities are fixed and never stored here (D3).
    Always resolved (never ``None``) -- see ``get_opportunity_config``.
    """

    currency: str
    stage_probabilities: dict[str, int] = field(default_factory=dict)


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Opportunity config is always tenant-scoped; a global caller has no
    tenant_id and therefore cannot be filtered to a tenant's row.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Opportunity config repository is tenant-scoped; PLATFORM_ADMIN "
            "callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_opportunity_config(db: Database, claims: AuthClaims) -> OpportunityConfig:
    """Fetch the caller's tenant opportunity config, or settings defaults.

    Never returns ``None`` -- an unconfigured tenant deterministically
    receives ``settings.opportunity_default_currency`` and
    ``opportunity_default_prob_*`` (D3/D7). Raises ``ValidationError`` for
    global callers.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT currency, stage_probabilities "
        "FROM tenant_opportunity_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    settings = get_api_settings()
    if row is None:
        return OpportunityConfig(
            currency=settings.opportunity_default_currency,
            stage_probabilities={
                "prospecting": settings.opportunity_default_prob_prospecting,
                "qualification": settings.opportunity_default_prob_qualification,
                "proposal": settings.opportunity_default_prob_proposal,
                "negotiation": settings.opportunity_default_prob_negotiation,
            },
        )

    raw_probabilities = dict(row["stage_probabilities"])
    return OpportunityConfig(
        currency=str(row["currency"]),
        stage_probabilities={k: int(v) for k, v in raw_probabilities.items()},
    )


async def upsert_opportunity_config(
    db: Database,
    claims: AuthClaims,
    *,
    currency: str,
    stage_probabilities: dict[str, int],
) -> None:
    """Insert or update the caller's tenant opportunity config.

    Raises ``ValidationError`` for global callers,
    ``ValidationError`` (``INVALID_CURRENCY``) if ``currency`` is not a
    3-uppercase-letter ISO-4217-shaped code (defense-in-depth over the DB
    CHECK constraint, validated in Python BEFORE the DB), and
    ``ValidationError`` (``INVALID_STAGE_PROBABILITIES``) if
    ``stage_probabilities`` contains an unknown stage key, a TERMINAL stage
    key (``closed_won``/``closed_lost`` are fixed, not tenant-configurable
    -- D3), or a value outside ``0..100``.
    """
    _reject_global(claims)

    if (
        len(currency) != _CURRENCY_RE_LEN
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValidationError(
            "currency must be a 3-uppercase-letter ISO-4217 code (e.g. 'USD').",
            code="INVALID_CURRENCY",
        )

    for stage, probability in stage_probabilities.items():
        if stage in _TERMINAL_STAGES:
            raise ValidationError(
                f"Stage {stage!r} probability is fixed and not tenant-configurable.",
                code="INVALID_STAGE_PROBABILITIES",
            )
        if stage not in _NON_TERMINAL_STAGES:
            raise ValidationError(
                f"Unrecognized opportunity stage {stage!r}.",
                code="INVALID_STAGE_PROBABILITIES",
            )
        if not (0 <= probability <= 100):
            raise ValidationError(
                f"Stage {stage!r} probability must be between 0 and 100.",
                code="INVALID_STAGE_PROBABILITIES",
            )

    await db.execute(
        "INSERT INTO tenant_opportunity_configs "
        "(tenant_id, currency, stage_probabilities) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "currency = $2, stage_probabilities = $3, updated_at = now()",
        claims.tenant_id,
        currency,
        stage_probabilities,
    )
