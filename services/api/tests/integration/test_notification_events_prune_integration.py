"""Integration test for D7's pruning task, against a REAL Postgres
connection: old events are deleted, recent ones are kept, and their
``notification_event_reads`` rows are removed via the migration's composite
``ON DELETE CASCADE`` -- not by any explicit DELETE in this module.

Marked ``integration``; skipped when ``TEST_DATABASE_URL`` is not set.
Safety-gated identically to the sibling integration test files in this
sprint (refuses to run if TEST_DATABASE_URL == DATABASE_URL/_DIRECT).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.db import Database

from api.notifications.events_repository import prune_events_older_than

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")

if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)

if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip(
        "TEST_DATABASE_URL is identical to DATABASE_URL/DATABASE_URL_DIRECT — "
        "refusing to run destructive integration tests against what may be "
        "the development database.",
        allow_module_level=True,
    )


@pytest.fixture
async def db() -> Any:
    assert _TEST_DB_URL is not None
    database = await Database.connect(_TEST_DB_URL, min_size=1, max_size=5, statement_cache_size=0)

    await database.execute("DROP TABLE IF EXISTS notification_event_reads")
    await database.execute("DROP TABLE IF EXISTS notification_events")
    await database.execute(
        """
        CREATE TABLE notification_events (
          tenant_id   text        NOT NULL,
          event_id    text        NOT NULL,
          kind        text        NOT NULL,
          category    text        NOT NULL,
          target_type text,
          target_id   text,
          payload     jsonb,
          actor_id    text,
          created_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, event_id)
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE notification_event_reads (
          tenant_id text        NOT NULL,
          event_id  text        NOT NULL,
          user_id   text        NOT NULL,
          read_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, event_id, user_id),
          FOREIGN KEY (tenant_id, event_id)
            REFERENCES notification_events (tenant_id, event_id) ON DELETE CASCADE
        )
        """
    )

    yield database

    await database.execute("DROP TABLE IF EXISTS notification_event_reads")
    await database.execute("DROP TABLE IF EXISTS notification_events")
    await database.close()


async def _insert_event(db: Database, *, tenant_id: str, created_at: datetime) -> str:
    event_id = uuid4().hex
    await db.execute(
        "INSERT INTO notification_events "
        "(tenant_id, event_id, kind, category, target_type, target_id, payload, actor_id, created_at) "
        "VALUES ($1, $2, 'lead_captured', 'leads', 'lead', $3, $4, NULL, $5)",
        tenant_id, event_id, "lead-x", {"lead_id": "lead-x"}, created_at,
    )
    return event_id


async def test_prune_deletes_old_events_and_cascades_read_rows_real_db(db: Database) -> None:
    tenant_id = uuid4().hex
    now = datetime.now(UTC)

    old_event_id = await _insert_event(db, tenant_id=tenant_id, created_at=now - timedelta(days=120))
    recent_event_id = await _insert_event(db, tenant_id=tenant_id, created_at=now - timedelta(days=5))

    # Read rows for BOTH events -- the old one must cascade-delete when its
    # event is pruned; the recent one must survive untouched.
    await db.execute(
        "INSERT INTO notification_event_reads (tenant_id, event_id, user_id, read_at) "
        "VALUES ($1, $2, $3, $4)",
        tenant_id, old_event_id, "user-x", now,
    )
    await db.execute(
        "INSERT INTO notification_event_reads (tenant_id, event_id, user_id, read_at) "
        "VALUES ($1, $2, $3, $4)",
        tenant_id, recent_event_id, "user-x", now,
    )

    deleted = await prune_events_older_than(db, retention_days=90)
    assert deleted == 1

    remaining_events = await db.fetch(
        "SELECT event_id FROM notification_events WHERE tenant_id = $1", tenant_id
    )
    remaining_ids = {r["event_id"] for r in remaining_events}
    assert remaining_ids == {recent_event_id}

    remaining_reads = await db.fetch(
        "SELECT event_id FROM notification_event_reads WHERE tenant_id = $1", tenant_id
    )
    remaining_read_event_ids = {r["event_id"] for r in remaining_reads}
    # The old event's read row must be GONE (cascade), the recent one's must remain.
    assert remaining_read_event_ids == {recent_event_id}


async def test_prune_is_idempotent_real_db(db: Database) -> None:
    tenant_id = uuid4().hex
    now = datetime.now(UTC)
    await _insert_event(db, tenant_id=tenant_id, created_at=now - timedelta(days=120))

    first = await prune_events_older_than(db, retention_days=90)
    second = await prune_events_older_than(db, retention_days=90)

    assert first == 1
    assert second == 0
