"""Plan 1.2/1.3 (owner, 2026-11-17): a deterministic, client-configured lead score.
Fit 40 / intent 30 / urgency 20 / engagement 10; an unanswered question counts as zero;
75+ high, 45-74 medium, under 45 low. The AI only maps an answer to a configured choice."""
import pytest
from pydantic import ValidationError

from api.qualification.scoring import (
    DEFAULT_CONFIG,
    QualConfig,
    band,
    engagement,
    next_question,
    score,
)

_TOP = {"jobs_per_month": "over 20", "marketing_spend": "over $5k", "decision_maker": "yes",
        "improve": "more leads", "start": "this month"}


def test_the_default_roofing_set() -> None:
    w = DEFAULT_CONFIG.weights
    assert (w.fit, w.intent, w.urgency, w.engagement) == (40, 30, 20, 10)
    assert [(q.key, q.category) for q in DEFAULT_CONFIG.questions] == [
        ("jobs_per_month", "fit"), ("marketing_spend", "fit"), ("decision_maker", "fit"),
        ("improve", "intent"), ("start", "urgency")]
    assert [c.label for c in DEFAULT_CONFIG.questions[4].choices] == [
        "this month", "in 1 to 3 months", "later"]


def test_nothing_known_is_zero_and_everything_best_is_a_hundred() -> None:
    assert score(DEFAULT_CONFIG, {}, 0) == 0
    assert score(DEFAULT_CONFIG, _TOP, 100) == 100


def test_the_weights_combine_each_category() -> None:
    # fit (100 + 70 + 100) / 3 = 90 -> 36; intent 100 -> 30; urgency 60 -> 12; engagement 40 -> 4
    answers = {**_TOP, "marketing_spend": "$1k to $5k", "start": "in 1 to 3 months"}
    assert score(DEFAULT_CONFIG, answers, 40) == 82


def test_an_unanswered_question_counts_as_zero() -> None:
    assert score(DEFAULT_CONFIG, {"start": "this month"}, 0) == 20
    # Only the fit questions answered, all best: 40 of 100 -- one strong area isn't "high".
    fit = {k: _TOP[k] for k in ("jobs_per_month", "marketing_spend", "decision_maker")}
    assert score(DEFAULT_CONFIG, fit, 0) == 40


def test_an_answer_that_is_no_longer_a_choice_counts_as_zero() -> None:
    assert score(DEFAULT_CONFIG, {"start": "next year"}, 0) == 0


@pytest.mark.parametrize(("value", "expected"), [
    (100, "high"), (75, "high"), (74, "medium"), (45, "medium"), (44, "low"), (0, "low")])
def test_bands(value: int, expected: str) -> None:
    assert band(value) == expected


def test_engagement_adds_up_and_caps_at_a_hundred() -> None:
    assert engagement(spoke=False, replied=False, chatted=False, asked_for_call=False) == 0
    assert engagement(spoke=True, replied=False, chatted=False, asked_for_call=False) == 40
    assert engagement(spoke=False, replied=True, chatted=True, asked_for_call=True) == 60
    assert engagement(spoke=True, replied=True, chatted=True, asked_for_call=True) == 100


def test_next_question_is_the_first_one_still_unanswered() -> None:
    first = next_question(DEFAULT_CONFIG, {})
    assert first is not None and first.key == "jobs_per_month"
    assert first.ask == "Roughly how many roofing jobs do you do a month?"
    nxt = next_question(DEFAULT_CONFIG, {"jobs_per_month": "over 20", "marketing_spend": "x"})
    assert nxt is not None and nxt.key == "decision_maker"
    assert next_question(DEFAULT_CONFIG, _TOP) is None


def _config(**over: object) -> dict[str, object]:
    body = DEFAULT_CONFIG.model_dump()
    body.update(over)
    return body


def test_a_config_is_checked_before_it_is_saved() -> None:
    QualConfig.model_validate(_config())  # the default is valid
    bad_weights = {"fit": 50, "intent": 30, "urgency": 20, "engagement": 10}
    with pytest.raises(ValidationError, match="add up to 100"):
        QualConfig.model_validate(_config(weights=bad_weights))
    one_choice = [{"key": "x", "ask": "Is this real?", "category": "fit",
                   "choices": [{"label": "yes", "points": 100}]}]
    with pytest.raises(ValidationError):
        QualConfig.model_validate(_config(questions=one_choice))
    q = {"key": "x", "ask": "Is this real?", "category": "fit",
         "choices": [{"label": "yes", "points": 100}, {"label": "no", "points": 0}]}
    with pytest.raises(ValidationError, match="unique"):
        QualConfig.model_validate(_config(questions=[q, q]))
    with pytest.raises(ValidationError):
        QualConfig.model_validate(_config(questions=[{**q, "category": "budget"}]))
    with pytest.raises(ValidationError):
        QualConfig.model_validate(_config(questions=[{**q, "choices": [
            {"label": "yes", "points": 101}, {"label": "no", "points": 0}]}]))
