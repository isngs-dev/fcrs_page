"""Integration test for D2 -- the most important behavior in SR-21: a feed
emit failure must NEVER cost a lead or fail the mutation it observes, proven
against a REAL Postgres connection (not a mocked exception), mirroring
SR-20 D3's proven pattern
(``test_lead_assignment_fail_open_integration.py``) exactly.

Marked ``integration``; skipped when ``TEST_DATABASE_URL`` is not set.
Safety-gated identically to the sibling integration test files in this
sprint (refuses to run if TEST_DATABASE_URL == DATABASE_URL/_DIRECT).
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.leads.repository import create_lead, get_lead
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

    # Deliberately NOT creating notification_events / notification_event_reads
    # -- this is exactly the "genuine error" case D2 requires emit_event_safe
    # to survive: the feed table is missing, so events_repository.emit_event's
    # very first INSERT raises a real asyncpg.UndefinedTableError.
    await database.execute("DROP TABLE IF EXISTS notification_event_reads")
    await database.execute("DROP TABLE IF EXISTS notification_events")
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.execute("DROP TABLE IF EXISTS tenants")
    await database.execute(
        """
        CREATE TABLE tenants (
            id text PRIMARY KEY, name text NOT NULL, slug text NOT NULL UNIQUE,
            enabled boolean NOT NULL DEFAULT true, timezone text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE leads (
            tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            lead_id text NOT NULL, visitor_id text, name text, email text, phone text,
            status text NOT NULL DEFAULT 'new', stage text NOT NULL DEFAULT 'captured',
            qualification_score int, consent jsonb NOT NULL DEFAULT '{}'::jsonb,
            assigned_agent_id text, source text NOT NULL DEFAULT 'widget',
            converted_to_contact_id text,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, lead_id)
        )
        """
    )

    yield database

    await database.execute("DROP TABLE IF EXISTS notification_event_reads")
    await database.execute("DROP TABLE IF EXISTS notification_events")
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.execute("DROP TABLE IF EXISTS tenants")
    await database.close()


async def _make_tenant(db: Database) -> str:
    tenant_id = uuid4().hex
    await db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id, "T", f"t-{tenant_id[:8]}",
    )
    return tenant_id


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="visitor-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


async def test_emit_failure_never_costs_a_lead_real_db(db: Database) -> None:
    """THE critical test (D2): create a real lead, then force the feed
    insert to fail with a REAL Postgres error (missing table) -- the lead
    must still exist afterward, and emit_event_safe must not raise."""
    tenant_id = await _make_tenant(db)
    claims = _claims(tenant_id)

    lead_id = await create_lead(
        db, claims, visitor_id="visitor-xyz", name="Jane", email="jane@example.com",
        phone=None, consent={"granted": True}, source="widget",
    )

    # Must not raise, even though notification_events does not exist.
    result = await emit_event_safe(
        db,
        claims,
        kind="lead_captured",
        category="leads",
        target_type="lead",
        target_id=lead_id,
        payload={"lead_id": lead_id},
        actor_id=None,
    )
    assert result is None  # fail-open: no event_id on failure

    lead = await get_lead(db, claims, lead_id)
    assert lead is not None
    assert lead.lead_id == lead_id


async def test_emit_failure_logs_warning_with_required_fields_real_db(
    db: Database, caplog: pytest.LogCaptureFixture,
) -> None:
    """D2: the swallowed failure is never silent -- a warning-level log line
    names event/tenant_id/kind, proven against the real asyncpg error path
    (not a mocked exception)."""
    tenant_id = await _make_tenant(db)
    claims = _claims(tenant_id)

    lead_id = await create_lead(
        db, claims, visitor_id="visitor-xyz", name="Jane", email="jane@example.com",
        phone=None, consent={"granted": True}, source="widget",
    )

    with caplog.at_level(logging.WARNING, logger="api.notifications.emit"):
        result = await emit_event_safe(
            db,
            claims,
            kind="lead_captured",
            category="leads",
            target_type="lead",
            target_id=lead_id,
            payload={"lead_id": lead_id},
            actor_id=None,
        )
    assert result is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a warning-level log line on emit failure"
    record = warnings[0]
    assert getattr(record, "kind", None) == "lead_captured"
    assert getattr(record, "event", None) == "notification_event_emit_failed"
    # PII discipline: never a lead name/email/phone in the log line.
    message = record.getMessage()
    assert "Jane" not in message
    assert "jane@example.com" not in message
