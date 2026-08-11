"""Integration test for D1/D8: the in-console notifications feed
(``notification_events``) and the outbound delivery ledger
(``notification_jobs``) are completely separate tables and code paths.
Emitting a feed event must write NO row to ``notification_jobs`` and send
nothing -- proven against a REAL Postgres connection with BOTH tables
present, so a query that accidentally joined or wrote across them would be
caught here.

Marked ``integration``; skipped when ``TEST_DATABASE_URL`` is not set.
Safety-gated identically to the sibling integration test files in this
sprint (refuses to run if TEST_DATABASE_URL == DATABASE_URL/_DIRECT).
"""
from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.notifications.emit import emit_event_safe

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
    await database.execute("DROP TABLE IF EXISTS notification_jobs")

    # notification_events -- the IN-CONSOLE feed (D1).
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

    # notification_jobs -- the OUTBOUND delivery ledger (mirrors
    # 0021_notifications.py's shape, widened by 0026/0041). Present here so a
    # feed emit that accidentally touched this table would be caught.
    await database.execute(
        """
        CREATE TABLE notification_jobs (
          job_id       text        NOT NULL PRIMARY KEY,
          tenant_id    text        NOT NULL,
          channel      text        NOT NULL,
          template     text,
          recipient    text        NOT NULL,
          subject      text        NOT NULL,
          body         text        NOT NULL,
          payload      jsonb,
          dedupe_key   text        NOT NULL,
          status       text        NOT NULL DEFAULT 'pending',
          attempts     integer     NOT NULL DEFAULT 0,
          delivery_ref text,
          last_error   text,
          lead_id      text,
          created_at   timestamptz NOT NULL DEFAULT now(),
          updated_at   timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, dedupe_key)
        )
        """
    )

    yield database

    await database.execute("DROP TABLE IF EXISTS notification_event_reads")
    await database.execute("DROP TABLE IF EXISTS notification_events")
    await database.execute("DROP TABLE IF EXISTS notification_jobs")
    await database.close()


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="visitor-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


async def test_emitting_a_feed_event_writes_no_row_to_notification_jobs(db: Database) -> None:
    """D1/D8: a feed emit never touches the outbound ledger and sends nothing."""
    tenant_id = uuid4().hex
    claims = _claims(tenant_id)

    before = await db.fetchval("SELECT count(*) FROM notification_jobs")
    assert before == 0

    event_id = await emit_event_safe(
        db,
        claims,
        kind="lead_captured",
        category="leads",
        target_type="lead",
        target_id="lead-xyz",
        payload={"lead_id": "lead-xyz"},
        actor_id=None,
    )
    assert event_id is not None  # the feed write itself succeeded

    after = await db.fetchval("SELECT count(*) FROM notification_jobs")
    assert after == 0, "a feed emit must never write to notification_jobs (D1/D8)"

    # And the feed row IS present, in notification_events, not notification_jobs.
    feed_rows = await db.fetch(
        "SELECT event_id, kind FROM notification_events WHERE tenant_id = $1", tenant_id
    )
    assert len(feed_rows) == 1
    assert feed_rows[0]["kind"] == "lead_captured"
