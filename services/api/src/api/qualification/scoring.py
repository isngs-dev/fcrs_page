"""The lead score: pure, deterministic, configured per client (plan 1.2/1.3, owner 2026-11-17).

Each question belongs to fit, intent or urgency and has fixed answer choices worth 0-100
points. A category scores the average of its questions' points -- an unanswered question
counts as zero, so "high" means well qualified AND well known. Engagement comes from what
the lead did (``engagement``), not from questions. The weights (default 40/30/20/10) add
up to 100. The AI only maps a spoken or typed answer onto one of the choices; the points
always come from here.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Category = Literal["fit", "intent", "urgency"]
Band = Literal["high", "medium", "low"]
HIGH, MEDIUM = 75, 45


class Choice(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    points: int = Field(ge=0, le=100)


class Question(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    ask: str = Field(min_length=5, max_length=200)
    category: Category
    choices: list[Choice] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def _distinct_choices(self) -> Question:
        labels = [c.label.lower() for c in self.choices]
        if len(set(labels)) != len(labels):
            raise ValueError("choice labels must be unique")
        return self


class Weights(BaseModel):
    fit: int = Field(default=40, ge=0, le=100)
    intent: int = Field(default=30, ge=0, le=100)
    urgency: int = Field(default=20, ge=0, le=100)
    engagement: int = Field(default=10, ge=0, le=100)

    @model_validator(mode="after")
    def _hundred(self) -> Weights:
        if self.fit + self.intent + self.urgency + self.engagement != 100:
            raise ValueError("the weights must add up to 100")
        return self


class QualConfig(BaseModel):
    weights: Weights = Field(default_factory=Weights)
    questions: list[Question] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _distinct_keys(self) -> QualConfig:
        keys = [q.key for q in self.questions]
        if len(set(keys)) != len(keys):
            raise ValueError("question keys must be unique")
        return self


def _q(key: str, ask: str, category: Category, *choices: tuple[str, int]) -> Question:
    return Question(key=key, ask=ask, category=category,
                    choices=[Choice(label=label, points=p) for label, p in choices])


# Owner-approved starting set for roofing clients (2026-11-17); each client can edit it.
DEFAULT_CONFIG = QualConfig(questions=[
    _q("jobs_per_month", "Roughly how many roofing jobs do you do a month?", "fit",
       ("under 5", 30), ("5 to 20", 70), ("over 20", 100)),
    _q("marketing_spend", "What do you spend on marketing a month?", "fit",
       ("under $1k", 30), ("$1k to $5k", 70), ("over $5k", 100)),
    _q("decision_maker", "Do you make the marketing decisions?", "fit",
       ("yes", 100), ("with a partner", 70), ("no", 30)),
    _q("improve", "What would you most like to improve?", "intent",
       ("more leads", 100), ("better-quality leads", 100), ("just exploring", 20)),
    _q("start", "When would you like to get started?", "urgency",
       ("this month", 100), ("in 1 to 3 months", 60), ("later", 20)),
])


def _points(question: Question, choice: str | None) -> int:
    return next((c.points for c in question.choices if c.label == choice), 0)


def score(config: QualConfig, answers: Mapping[str, str], engagement: int) -> int:
    """0-100. ``answers`` maps a question key to the chosen label."""
    w = config.weights
    total = float(w.engagement * engagement)
    for category, weight in (("fit", w.fit), ("intent", w.intent), ("urgency", w.urgency)):
        points = [_points(q, answers.get(q.key)) for q in config.questions
                  if q.category == category]
        if points:
            total += weight * sum(points) / len(points)
    return round(total / 100)


def band(value: int) -> Band:
    return "high" if value >= HIGH else "medium" if value >= MEDIUM else "low"


def engagement(*, spoke: bool, replied: bool, chatted: bool, asked_for_call: bool) -> int:
    """Spoke with Rebecca 40, replied to an email 30, chatted on the site 20, asked to be
    called 10 -- capped at 100."""
    return min(100, 40 * spoke + 30 * replied + 20 * chatted + 10 * asked_for_call)


def next_question(config: QualConfig, answers: Mapping[str, str]) -> Question | None:
    return next((q for q in config.questions if q.key not in answers), None)
