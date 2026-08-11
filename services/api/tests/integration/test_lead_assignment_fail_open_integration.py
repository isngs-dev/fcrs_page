"""Integration test for D3 -- the single most important behavior in this
sprint: an assignment failure must NEVER cost a lead, proven against a
REAL Postgres connection (not a mocked exception).

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

from api.leads.assignment import assign_lead_fail_open
from api.leads.repository import create_lead, get_lead

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

    await database.execute("DROP TABLE IF EXISTS tenant_assignment_configs")
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.execute("DROP TABLE IF EXISTS users")
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
        CREATE TABLE users (
            id text PRIMARY KEY, tenant_id text REFERENCES tenants(id) ON DELETE CASCADE,
            email text NOT NULL,
            role text NOT NULL CHECK (role IN ('PLATFORM_ADMIN','CLIENT_ADMIN','CLIENT_AGENT')),
            password_hash text NOT NULL, name text, active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
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
    # Deliberately NOT creating tenant_assignment_configs -- this is exactly
    # the "genuine error" case D3 requires assign_lead_fail_open to survive:
    # the config table is missing, so select_next_agent's very first query
    # raises a real asyncpg.UndefinedTableError.

    yield database

    await database.execute("DROP TABLE IF EXISTS tenant_assignment_configs")
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.execute("DROP TABLE IF EXISTS users")
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


async def test_assignment_failure_never_costs_a_lead_real_db(db: Database) -> None:
    """THE critical test (D3): create a real lead, then force the assignment
    query to fail with a REAL Postgres error (missing table) -- the lead
    must still exist afterward, unassigned, and assign_lead_fail_open must
    not raise."""
    tenant_id = await _make_tenant(db)
    claims = _claims(tenant_id)

    lead_id = await create_lead(
        db, claims, visitor_id="visitor-xyz", name="Jane", email="jane@example.com",
        phone=None, consent={"granted": True}, source="widget",
    )

    # Must not raise, even though tenant_assignment_configs does not exist.
    await assign_lead_fail_open(db, claims, lead_id=lead_id)

    lead = await get_lead(db, claims, lead_id)
    assert lead is not None
    assert lead.assigned_agent_id is None
