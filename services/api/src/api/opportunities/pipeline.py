"""Opportunity pipeline -- pure stage state machine + derived win-probability.

No I/O. Everything here is a pure function so it can be unit-tested without a
database. Mirrors ``leads/pipeline.py``'s shape (SR-9.4 D1/M4) but is a
SEPARATE module -- the lead funnel is not modified, extended, or imported.

Stage state machine (SR-9.4 D1)
--------------------------------
``STAGE_ORDER`` is the forward funnel. From a non-terminal stage ``S`` the
only legal transitions are:

- to the immediate next stage in ``STAGE_ORDER`` (forward one step), or
- to ``"closed_lost"`` (an off-ramp available from any non-terminal stage).

Skipping a stage, moving backward, a no-op (``X -> X``), or any transition
*out of* a terminal stage (``TERMINAL_STAGES``) is illegal and raises
``ValidationError`` (``INVALID_OPPORTUNITY_STAGE_TRANSITION`` -- a code
DISTINCT from the lead funnel's ``INVALID_STAGE_TRANSITION``, D5's
no-reopen guarantee applies to every terminal stage for every target,
including back to ``prospecting``).

Win-probability derivation (SR-9.4 D2/D3)
-------------------------------------------
``win_probability_for_stage`` is a pure function of ``(stage, config)``.
It is NEVER stored on the opportunity row -- changing a tenant's config
changes the displayed probability of every existing open opportunity with
zero writes to any row. Terminal stages (``closed_won``/``closed_lost``)
always return the fixed 100/0 regardless of what the tenant config
contains (D3 -- terminal probabilities are not tenant-configurable).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from common.errors import ValidationError

if TYPE_CHECKING:
    from api.opportunities.config_repository import OpportunityConfig

STAGE_ORDER: list[str] = ["prospecting", "qualification", "proposal", "negotiation", "closed_won"]
"""The forward funnel, in order."""

TERMINAL_STAGES: set[str] = {"closed_won", "closed_lost"}
"""Stages from which no further transition is permitted (D5 -- permanent)."""

_TERMINAL_WIN_PROBABILITY: dict[str, int] = {
    "closed_won": 100,
    "closed_lost": 0,
}
"""Fixed, non-tenant-configurable terminal probabilities (D3)."""


def validate_transition(current: str, target: str) -> None:
    """Validate a proposed stage transition, raising on illegal moves.

    Raises ``ValidationError`` (code ``INVALID_OPPORTUNITY_STAGE_TRANSITION``)
    unless ``target`` is either the immediate next stage after ``current`` in
    ``STAGE_ORDER``, or ``"closed_lost"`` from a non-terminal ``current``.
    No transition out of a terminal stage is ever legal (D5).
    """
    if current in TERMINAL_STAGES:
        _raise_invalid(current, target)

    if target == "closed_lost":
        return

    if current not in STAGE_ORDER or target not in STAGE_ORDER:
        _raise_invalid(current, target)
        return

    current_index = STAGE_ORDER.index(current)
    target_index = STAGE_ORDER.index(target)
    if target_index != current_index + 1:
        _raise_invalid(current, target)


def _raise_invalid(current: str, target: str) -> None:
    raise ValidationError(
        f"Illegal opportunity stage transition from {current!r} to {target!r}.",
        code="INVALID_OPPORTUNITY_STAGE_TRANSITION",
    )


def win_probability_for_stage(stage: str, config: OpportunityConfig) -> int:
    """Return the derived win-probability for ``stage`` under ``config``.

    Terminal stages (``closed_won``/``closed_lost``) always return the fixed
    100/0, ignoring whatever the tenant config contains (D3's fixed-terminal
    guarantee). Non-terminal stages resolve from
    ``config.stage_probabilities``. Raises ``ValidationError`` for an
    unrecognized stage (defensive; callers should only ever pass a stage
    that already passed ``validate_transition`` or is a row's persisted
    ``stage``).
    """
    if stage in _TERMINAL_WIN_PROBABILITY:
        return _TERMINAL_WIN_PROBABILITY[stage]

    try:
        return config.stage_probabilities[stage]
    except KeyError:
        raise ValidationError(
            f"Unrecognized opportunity stage {stage!r}.",
            code="INVALID_OPPORTUNITY_STAGE_TRANSITION",
        ) from None
