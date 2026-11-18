"""Outreach plan types (``specs/agents.md`` §2)."""
from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

TaskChannel = Literal["email", "call", "sms"]
TaskGoal = Literal[
    "follow_up", "answer_questions", "qualify", "book_meeting", "reminder", "re_engage",
    "thank_you", "acknowledge",
]
PlanKind = Literal["inbound_auto", "outbound_manual", "outbound_csv", "no_show_recovery",
                   "after_meeting", "nurture"]  # Phase 2 kinds (migration 0080)


class AgentTask(BaseModel):
    channel: TaskChannel
    offset: timedelta  # from plan start
    goal: TaskGoal
    intent: str
    max_attempts: int = Field(default=1, ge=1, le=5)


class ConsentRef(BaseModel):
    consent_id: str
    basis: str


class OutreachPlan(BaseModel):
    kind: PlanKind
    steps: list[AgentTask]
    # Resolved from consent records by the planner -- never supplied by an LLM.
    lawful_basis: dict[TaskChannel, ConsentRef] = Field(default_factory=dict)


class PolicyNote(BaseModel):
    step_index: int | None
    code: str
    detail: str = ""
