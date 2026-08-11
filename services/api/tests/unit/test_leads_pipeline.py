"""Unit tests for api.leads.pipeline -- pure stage state machine + scoring.

Covers:
- validate_transition: valid forward steps, valid disqualify off-ramps,
  invalid skip/backward/no-op/out-of-terminal transitions.
- status_for_stage: mapping for all 5 stages.
- compute_qualification_score: determinism, weight contributions, clamping.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from common.errors import ValidationError

from api.leads.pipeline import (
    STAGE_ORDER,
    STAGE_SORT_POSITION,
    STATUS_SORT_POSITION,
    TERMINAL_STAGES,
    compute_qualification_score,
    status_for_stage,
    validate_transition,
)
from api.leads.repository import Lead

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _lead(
    *,
    stage: str = "captured",
    email: str = "jane@example.com",
    phone: str | None = "+1555123456",
    name: str = "Jane Doe",
    source: str = "widget",
) -> Lead:
    return Lead(
        lead_id="lead-1",
        visitor_id="visitor-1",
        name=name,
        email=email,
        phone=phone,
        status=status_for_stage(stage),
        stage=stage,
        qualification_score=None,
        consent={"granted": True, "purpose": "contact", "text": "OK"},
        assigned_agent_id=None,
        source=source,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# validate_transition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("captured", "qualified"),
        ("qualified", "contacted"),
        ("contacted", "converted"),
    ],
)
def test_validate_transition_valid_forward_step(current: str, target: str) -> None:
    """Moving one step forward in STAGE_ORDER is valid (no raise)."""
    validate_transition(current, target)


@pytest.mark.parametrize("current", ["captured", "qualified", "contacted"])
def test_validate_transition_disqualify_from_non_terminal(current: str) -> None:
    """Disqualifying from any non-terminal stage is a valid off-ramp."""
    validate_transition(current, "disqualified")


def test_validate_transition_skip_stage_raises() -> None:
    """Skipping a stage (captured -> converted) is invalid."""
    with pytest.raises(ValidationError) as exc_info:
        validate_transition("captured", "converted")
    assert exc_info.value.code == "INVALID_STAGE_TRANSITION"


def test_validate_transition_backward_raises() -> None:
    """Moving backward (qualified -> captured) is invalid."""
    with pytest.raises(ValidationError) as exc_info:
        validate_transition("qualified", "captured")
    assert exc_info.value.code == "INVALID_STAGE_TRANSITION"


@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_validate_transition_no_op_raises(stage: str) -> None:
    """X -> X (no-op) is invalid for every stage."""
    with pytest.raises(ValidationError) as exc_info:
        validate_transition(stage, stage)
    assert exc_info.value.code == "INVALID_STAGE_TRANSITION"


@pytest.mark.parametrize("target", ["captured", "qualified", "contacted", "converted", "disqualified"])
def test_validate_transition_out_of_converted_raises(target: str) -> None:
    """Any transition out of the terminal 'converted' stage is invalid."""
    with pytest.raises(ValidationError) as exc_info:
        validate_transition("converted", target)
    assert exc_info.value.code == "INVALID_STAGE_TRANSITION"


@pytest.mark.parametrize("target", ["captured", "qualified", "contacted", "converted", "disqualified"])
def test_validate_transition_out_of_disqualified_raises(target: str) -> None:
    """Any transition out of the terminal 'disqualified' stage is invalid."""
    with pytest.raises(ValidationError) as exc_info:
        validate_transition("disqualified", target)
    assert exc_info.value.code == "INVALID_STAGE_TRANSITION"


def test_validate_transition_unknown_stage_raises() -> None:
    """An unrecognized stage name is invalid."""
    with pytest.raises(ValidationError) as exc_info:
        validate_transition("captured", "not-a-real-stage")
    assert exc_info.value.code == "INVALID_STAGE_TRANSITION"


def test_terminal_stages_contents() -> None:
    assert TERMINAL_STAGES == {"converted", "disqualified"}


def test_stage_order_contents() -> None:
    assert STAGE_ORDER == ["captured", "qualified", "contacted", "converted"]


def test_stage_sort_position_covers_the_funnel_and_terminal_off_ramp() -> None:
    """SR-25 D2: sorting follows the canonical pipeline, never a SQL copy."""
    assert STAGE_SORT_POSITION == {
        "captured": 1,
        "qualified": 2,
        "contacted": 3,
        "converted": 4,
        "disqualified": 5,
    }


def test_status_sort_position_covers_every_derived_status() -> None:
    assert STATUS_SORT_POSITION == {"new": 1, "open": 2, "won": 3, "lost": 4}


# ---------------------------------------------------------------------------
# status_for_stage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "expected_status"),
    [
        ("captured", "new"),
        ("qualified", "open"),
        ("contacted", "open"),
        ("converted", "won"),
        ("disqualified", "lost"),
    ],
)
def test_status_for_stage_mapping(stage: str, expected_status: str) -> None:
    assert status_for_stage(stage) == expected_status


# ---------------------------------------------------------------------------
# compute_qualification_score
# ---------------------------------------------------------------------------


def test_compute_qualification_score_deterministic() -> None:
    """Same lead -> same score, computed repeatedly."""
    lead = _lead(stage="qualified")
    assert compute_qualification_score(lead) == compute_qualification_score(lead)


def test_compute_qualification_score_full_contact_widget_captured() -> None:
    """email(+30) + phone(+25) + name(+15) + widget(+10) + captured(+0) = 80."""
    lead = _lead(stage="captured", source="widget")
    assert compute_qualification_score(lead) == 80


def test_compute_qualification_score_missing_phone_lowers_score() -> None:
    with_phone = _lead(stage="captured", phone="+1555123456")
    without_phone = _lead(stage="captured", phone=None)
    assert compute_qualification_score(without_phone) < compute_qualification_score(with_phone)


def test_compute_qualification_score_missing_email_lowers_score() -> None:
    with_email = _lead(stage="captured", email="jane@example.com")
    without_email = _lead(stage="captured", email="")
    assert compute_qualification_score(without_email) < compute_qualification_score(with_email)


def test_compute_qualification_score_missing_name_lowers_score() -> None:
    with_name = _lead(stage="captured", name="Jane Doe")
    without_name = _lead(stage="captured", name="")
    assert compute_qualification_score(without_name) < compute_qualification_score(with_name)


def test_compute_qualification_score_referral_beats_widget() -> None:
    referral = _lead(stage="captured", source="referral")
    widget = _lead(stage="captured", source="widget")
    assert compute_qualification_score(referral) > compute_qualification_score(widget)


def test_compute_qualification_score_other_source_scores_zero_bonus() -> None:
    other = _lead(stage="captured", source="organic")
    widget = _lead(stage="captured", source="widget")
    assert compute_qualification_score(other) < compute_qualification_score(widget)


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("captured", "qualified"),
        ("qualified", "contacted"),
        ("contacted", "converted"),
    ],
)
def test_compute_qualification_score_later_stage_raises_score(earlier: str, later: str) -> None:
    earlier_lead = _lead(stage=earlier)
    later_lead = _lead(stage=later)
    assert compute_qualification_score(later_lead) > compute_qualification_score(earlier_lead)


def test_compute_qualification_score_disqualified_has_no_stage_bonus() -> None:
    disqualified = _lead(stage="disqualified")
    captured = _lead(stage="captured")
    assert compute_qualification_score(disqualified) == compute_qualification_score(captured)


def test_compute_qualification_score_clamped_to_100_max() -> None:
    lead = _lead(stage="converted", source="referral")
    assert compute_qualification_score(lead) <= 100


def test_compute_qualification_score_clamped_to_0_min() -> None:
    lead = _lead(stage="disqualified", email="", phone=None, name="", source="organic")
    assert compute_qualification_score(lead) == 0


def test_compute_qualification_score_never_exceeds_100() -> None:
    lead = _lead(stage="converted", source="referral", email="jane@example.com", phone="+1", name="Jane")
    score = compute_qualification_score(lead)
    assert 0 <= score <= 100
