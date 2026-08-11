"""Unit tests for api.notifications.events_repository.prune_events_older_than
(SR-21 D7). A minimal, dedicated stub -- separate from
test_notification_events_repository.py's stub, which is shaped around the
list/emit/mark-read query text and would need extra branching to also
recognize a DELETE ... RETURNING statement.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from api.notifications.events_repository import prune_events_older_than

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _PruneStubDatabase:
    """Tracks rows by created_at age; DELETE ... RETURNING removes and
    returns the ones older than the interval arg."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        assert q.startswith("DELETE FROM NOTIFICATION_EVENTS")
        assert "RETURNING EVENT_ID" in q
        (retention_days,) = args
        cutoff = _NOW - timedelta(days=retention_days)
        to_delete = [r for r in self._rows if r["created_at"] < cutoff]
        self._rows = [r for r in self._rows if r["created_at"] >= cutoff]
        return [{"event_id": r["event_id"]} for r in to_delete]


async def test_prune_deletes_only_rows_past_the_window() -> None:
    rows = [
        {"event_id": "old-1", "created_at": _NOW - timedelta(days=120)},
        {"event_id": "old-2", "created_at": _NOW - timedelta(days=91)},
        {"event_id": "recent-1", "created_at": _NOW - timedelta(days=5)},
    ]
    db = _PruneStubDatabase(rows)

    deleted = await prune_events_older_than(db, retention_days=90)  # type: ignore[arg-type]

    assert deleted == 2
    remaining_ids = {r["event_id"] for r in db._rows}
    assert remaining_ids == {"recent-1"}


async def test_prune_is_idempotent_second_run_deletes_nothing() -> None:
    rows = [{"event_id": "old-1", "created_at": _NOW - timedelta(days=120)}]
    db = _PruneStubDatabase(rows)

    first = await prune_events_older_than(db, retention_days=90)  # type: ignore[arg-type]
    second = await prune_events_older_than(db, retention_days=90)  # type: ignore[arg-type]

    assert first == 1
    assert second == 0


async def test_prune_with_nothing_past_window_deletes_nothing() -> None:
    rows = [{"event_id": "recent-1", "created_at": _NOW - timedelta(days=1)}]
    db = _PruneStubDatabase(rows)

    deleted = await prune_events_older_than(db, retention_days=90)  # type: ignore[arg-type]

    assert deleted == 0
