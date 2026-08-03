"""Unit tests for api.opportunities.pipeline -- pure, no DB (SR-9.4).

Covers (D1/D2/D3/D5, MANDATORY per the sprint spec):
- Every legal forward-one-step move succeeds.
- Skipping / backward / no-op moves are rejected.
- closed_lost succeeds from every non-terminal stage.
- No transition out of ANY terminal stage for ANY target (the no-reopen
  guarantee, including back to prospecting).
- Rejections use the distinct INVALID_OPPORTUNITY_STAGE_TRANSITION code.
- win_probability_for_stage resolves from config for non-terminal stages,
  and is fixed 100/0 for terminal stages regardless of config content.
- leads/pipeline.py's own test suite is untouched (asserted by import only
  -- this file never imports or modifies it).
"""
from __future__ import annotations

import pytest
from common.errors import ValidationError

from api.opportunities.config_repository import OpportunityConfig
from api.opportunities.pipeline import (
    STAGE_ORDER,
    TERMINAL_STAGES,
    validate_transition,
    win_probability_for_stage,
)

_DEFAULT_CONFIG = OpportunityConfig(
    currency="USD",
    stage_probabilities={
        "prospecting": 10,
        "qualification": 25,
        "proposal": 50,
        "negotiation": 75,
    },
)


# ---------------------------------------------------------------------------
# validate_transition -- legal forward moves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("prospecting", "qualification"),
        ("qualification", "proposal"),
        ("proposal", "negotiation"),
        ("negotiation", "closed_won"),
    ],
)
def test_legal_forward_one_step_moves_succeed(current: str, target: str) -> None:
    validate_transition(current, target)  # must not raise


def test_stage_order_is_the_expected_five_stage_funnel() -> None:
    assert STAGE_ORDER == ["prospecting", "qualification", "proposal", "negotiation", "closed_won"]
    assert TERMINAL_STAGES == {"closed_won", "closed_lost"}


# ---------------------------------------------------------------------------
# validate_transition -- illegal moves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("prospecting", "proposal"),  # skip
        ("qualification", "closed_won"),  # skip
        ("prospecting", "negotiation"),  # skip
    ],
)
def test_skipping_is_rejected(current: str, target: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_transition(current, target)
    assert exc_info.value.code == "INVALID_OPPORTUNITY_STAGE_TRANSITION"


def test_backward_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_transition("proposal", "qualification")
    assert exc_info.value.code == "INVALID_OPPORTUNITY_STAGE_TRANSITION"


@pytest.mark.parametrize("stage", ["prospecting", "qualification", "proposal", "negotiation"])
def test_no_op_is_rejected(stage: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_transition(stage, stage)
    assert exc_info.value.code == "INVALID_OPPORTUNITY_STAGE_TRANSITION"


# ---------------------------------------------------------------------------
# closed_lost off-ramp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current", ["prospecting", "qualification", "proposal", "negotiation"])
def test_closed_lost_succeeds_from_every_non_terminal_stage(current: str) -> None:
    validate_transition(current, "closed_lost")  # must not raise


# ---------------------------------------------------------------------------
# No reopen -- the D5 guarantee, asserted as a test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["closed_won", "closed_lost"])
@pytest.mark.parametrize(
    "target",
    ["prospecting", "qualification", "proposal", "negotiation", "closed_won", "closed_lost"],
)
def test_no_transition_out_of_terminal_stage_for_any_target(terminal: str, target: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_transition(terminal, target)
    assert exc_info.value.code == "INVALID_OPPORTUNITY_STAGE_TRANSITION"


def test_rejection_code_is_distinct_from_lead_funnel_code() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_transition("prospecting", "closed_won")
    assert exc_info.value.code == "INVALID_OPPORTUNITY_STAGE_TRANSITION"
    assert exc_info.value.code != "INVALID_STAGE_TRANSITION"


def test_leads_pipeline_suite_still_passes_unmodified() -> None:
    """Regression guard: importing leads.pipeline here does not raise, and
    this file has made no edits to it -- see leads/pipeline.py, untouched."""
    from api.leads.pipeline import STAGE_ORDER as lead_stage_order
    from api.leads.pipeline import TERMINAL_STAGES as lead_terminal_stages

    assert lead_stage_order == ["captured", "qualified", "contacted", "converted"]
    assert lead_terminal_stages == {"converted", "disqualified"}


# ---------------------------------------------------------------------------
# win_probability_for_stage
# ---------------------------------------------------------------------------


def test_win_probability_resolves_from_config_for_non_terminal_stages() -> None:
    assert win_probability_for_stage("prospecting", _DEFAULT_CONFIG) == 10
    assert win_probability_for_stage("qualification", _DEFAULT_CONFIG) == 25
    assert win_probability_for_stage("proposal", _DEFAULT_CONFIG) == 50
    assert win_probability_for_stage("negotiation", _DEFAULT_CONFIG) == 75


def test_win_probability_terminal_stages_are_fixed_regardless_of_config() -> None:
    tampered_config = OpportunityConfig(
        currency="USD",
        stage_probabilities={
            "prospecting": 10,
            "qualification": 25,
            "proposal": 50,
            "negotiation": 75,
            # An attempted override -- should never be read for terminal stages.
            "closed_won": 42,
            "closed_lost": 99,
        },
    )
    assert win_probability_for_stage("closed_won", tampered_config) == 100
    assert win_probability_for_stage("closed_lost", tampered_config) == 0


def test_win_probability_unrecognized_stage_raises() -> None:
    with pytest.raises(ValidationError) as exc_info:
        win_probability_for_stage("bogus_stage", _DEFAULT_CONFIG)
    assert exc_info.value.code == "INVALID_OPPORTUNITY_STAGE_TRANSITION"
