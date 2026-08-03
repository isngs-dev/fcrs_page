"""Response models for the SR-9.3 customer-360 timeline.

Leak-free (CLAUDE.md §3): the response body never contains ``tenant_id``,
and a ``notification`` item's ``data`` never contains ``body`` (rendered
notification bodies carry customer PII and have no place in a list
projection -- only ``subject``/``status`` are surfaced).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

SourceState = Literal["ok", "unavailable"]
SourceName = Literal["conversations", "lead_activities", "bookings", "notifications"]
ItemKind = Literal["message", "lead_activity", "booking", "notification"]


@dataclass(frozen=True)
class TimelineItem:
    """A single normalized event, internal to the fan-out/merge (``service.py``).

    ``sort_key`` is the ``(occurred_at, source_kind, native_id)`` stable
    tiebreaker D9 requires: two items sharing an ``occurred_at`` must order
    deterministically and never duplicate/skip across pages.
    """

    kind: ItemKind
    occurred_at: datetime
    item_id: str
    data: dict[str, Any]

    @property
    def sort_key(self) -> tuple[datetime, str, str]:
        return (self.occurred_at, self.kind, self.item_id)


@dataclass(frozen=True)
class SourceOutcome:
    """Per-source fan-out result (D6) -- always present, success or failure."""

    state: SourceState
    count: int = 0
    truncated: bool | None = None


@dataclass(frozen=True)
class TimelineResult:
    """The internal result of ``service.build_timeline`` before serialization."""

    degraded: bool
    sources: dict[SourceName, SourceOutcome]
    items: list[TimelineItem]
    next_before: datetime | None


# ---------------------------------------------------------------------------
# HTTP response models (Pydantic) -- what actually goes over the wire.
# ---------------------------------------------------------------------------


class TimelineSubject(BaseModel):
    """Identifies whose timeline this is. Never carries tenant_id."""

    kind: Literal["contact", "lead"]
    id: str
    converted_to_contact_id: str | None = None


class TimelineSourceState(BaseModel):
    state: SourceState
    count: int = 0
    truncated: bool | None = None


class TimelineItemResponse(BaseModel):
    kind: ItemKind
    occurred_at: datetime
    id: str
    data: dict[str, Any]


class TimelineResponse(BaseModel):
    subject: TimelineSubject
    degraded: bool
    sources: dict[str, TimelineSourceState]
    items: list[TimelineItemResponse]
    next_before: datetime | None = None
