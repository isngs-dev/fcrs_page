"""Integration tests for round-robin lead assignment against a REAL Postgres
(SR-20 D1/D3/D4) -- the sprint's highest-risk piece, proven against real
schema/constraints/concurrency rather than a stub DB.

Marked ``integration``; skipped when ``TEST_DATABASE_URL`` is not set (same
gate as ``test_tenant_isolation.py``). Builds the exact tables this sprint's
migration (0045) adds, plus the minimal ``users``/``leads`` shape from
migrations 0002/0014, directly via DDL (not by running Alembic) -- mirrors
the existing integration test's own pattern.

SAFETY: this connects to ``TEST_DATABASE_URL`` ONLY, never
``DATABASE_URL``/``DATABASE_URL_DIRECT`` -- verified distinct from the dev
DB before use (``chatbot_test`` vs `chatbot`, per the sprint's mandatory
pre-flight check). The fixture drops and recreates its own tables inside
that test database only.

Covers (per the spec's Definition of Done + mandatory test list):
- Cross-tenant isolation: round-robin in tenant A never assigns tenant B's
  agents, and A's rotation cursor does not advance B's (the highest-value
  isolation test in this sprint).
- Real round-robin sequence over >= 2 full cycles (A, B, A, B for a
  2-agent pool).
- Inactive agents are never assigned; PLATFORM_ADMIN rows are never
  candidates.
- Zero active agents -> the lead is created, unassigned, no error (the
  ordinary case).
- Concurrency (D4): N simultaneous calls to select_next_agent for the same
  tenant never return duplicate agents beyond what full rotation implies --
  proven via real asyncio.gather against real Postgres row-locking, not a
  mocked/simulated sequence.
"""
from __future__ import annotations

import asyncio
import os
from collections import Counter
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.leads.assignment import select_next_agent
from api.leads.assignment_config_repository import upsert_assignment_config

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")

if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)

# Mandatory safety check (sprint instruction #7): TEST_DATABASE_URL must be
# genuinely distinct from the dev database. Never proceed if they collide --
# this project has a prior incident where a background agent ran integration
# tests against the real dev DB and dropped a production table.
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip(
        "TEST_DATABASE_URL is identical to DATABASE_URL/DATABASE_URL_DIRECT — "
        "refusing to run destructive integration tests against what may be "
        "the development database.",
        allow_module_level=True,
    )


@pytest.fixture
async def db() -> Any:
    """Create a live Database, set up the minimal tenants/users/leads/
    tenant_assignment_configs schema from migrations 0002/0014/0045, yield,
    then tear down. Scoped to TEST_DATABASE_URL only (verified above)."""
    assert _TEST_DB_URL is not None
    database = await Database.connect(_TEST_DB_URL, min_size=1, max_size=5, statement_cache_size=0)

    await database.execute("DROP TABLE IF EXISTS tenant_assignment_configs")
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.execute("DROP TABLE IF EXISTS users")
    await database.execute("DROP TABLE IF EXISTS tenants")
    await database.execute(
        """
        CREATE TABLE tenants (
            id          text PRIMARY KEY,
            name        text NOT NULL,
            slug        text NOT NULL UNIQUE,
            enabled     boolean NOT NULL DEFAULT true,
            timezone    text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE users (
            id              text PRIMARY KEY,
            tenant_id       text REFERENCES tenants(id) ON DELETE CASCADE,
            email           text NOT NULL,
            role            text NOT NULL
                CHECK (role IN ('PLATFORM_ADMIN', 'CLIENT_ADMIN', 'CLIENT_AGENT')),
            password_hash   text NOT NULL,
            name            text,
            active          boolean NOT NULL DEFAULT true,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE leads (
            tenant_id             text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            lead_id               text NOT NULL,
            visitor_id            text,
            name                  text,
            email                 text,
            phone                 text,
            status                text NOT NULL DEFAULT 'new',
            stage                 text NOT NULL DEFAULT 'captured',
            qualification_score   int,
            consent               jsonb NOT NULL DEFAULT '{}'::jsonb,
            assigned_agent_id     text,
            source                text NOT NULL DEFAULT 'widget',
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, lead_id)
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE tenant_assignment_configs (
            tenant_id               text PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            round_robin_enabled     boolean     NOT NULL DEFAULT false,
            last_assigned_agent_id  text,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    yield database

    await database.execute("DROP TABLE IF EXISTS tenant_assignment_configs")
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.execute("DROP TABLE IF EXISTS users")
    await database.execute("DROP TABLE IF EXISTS tenants")
    await database.close()


async def _make_tenant(db: Database, *, name: str) -> str:
    tenant_id = uuid4().hex
    await db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id, name, f"{name.lower()}-{tenant_id[:8]}",
    )
    return tenant_id


async def _make_agent(
    db: Database, *, tenant_id: str, role: str = "CLIENT_AGENT", active: bool = True,
) -> str:
    user_id = uuid4().hex
    await db.execute(
        "INSERT INTO users (id, tenant_id, email, role, password_hash, active) "
        "VALUES ($1, $2, $3, $4, 'x', $5)",
        user_id, tenant_id, f"{user_id[:8]}@example.com", role, active,
    )
    return user_id


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


# -- Cross-tenant isolation (the highest-value test in this sprint) ---------


async def test_round_robin_never_assigns_across_tenants(db: Database) -> None:
    tenant_a = await _make_tenant(db, name="TenantA")
    tenant_b = await _make_tenant(db, name="TenantB")
    agent_a = await _make_agent(db, tenant_id=tenant_a)
    agent_b = await _make_agent(db, tenant_id=tenant_b)

    await upsert_assignment_config(db, _claims(tenant_a), round_robin_enabled=True)
    await upsert_assignment_config(db, _claims(tenant_b), round_robin_enabled=True)

    for _ in range(4):
        chosen = await select_next_agent(db, _claims(tenant_a))
        assert chosen == agent_a
        assert chosen != agent_b

    for _ in range(4):
        chosen = await select_next_agent(db, _claims(tenant_b))
        assert chosen == agent_b
        assert chosen != agent_a


async def test_tenant_a_enabling_does_not_enable_tenant_b(db: Database) -> None:
    tenant_a = await _make_tenant(db, name="TenantA")
    tenant_b = await _make_tenant(db, name="TenantB")
    await _make_agent(db, tenant_id=tenant_a)
    await _make_agent(db, tenant_id=tenant_b)

    await upsert_assignment_config(db, _claims(tenant_a), round_robin_enabled=True)
    # Tenant B never opted in.

    result_b = await select_next_agent(db, _claims(tenant_b))
    assert result_b is None


# -- Real round-robin sequence (D1/D4) ---------------------------------------


async def test_rotation_cycles_over_two_full_cycles_real_db(db: Database) -> None:
    tenant = await _make_tenant(db, name="Rotator")
    agent_a = await _make_agent(db, tenant_id=tenant)
    agent_b = await _make_agent(db, tenant_id=tenant)
    await upsert_assignment_config(db, _claims(tenant), round_robin_enabled=True)

    # ORDER BY id determines sequence; discover the expected order directly.
    ordered = sorted([agent_a, agent_b])

    sequence = [await select_next_agent(db, _claims(tenant)) for _ in range(4)]

    assert sequence == [ordered[0], ordered[1], ordered[0], ordered[1]]


async def test_inactive_agents_never_assigned(db: Database) -> None:
    tenant = await _make_tenant(db, name="Inactive")
    active_agent = await _make_agent(db, tenant_id=tenant, active=True)
    await _make_agent(db, tenant_id=tenant, active=False)
    await upsert_assignment_config(db, _claims(tenant), round_robin_enabled=True)

    for _ in range(3):
        chosen = await select_next_agent(db, _claims(tenant))
        assert chosen == active_agent


async def test_platform_admin_role_never_a_candidate(db: Database) -> None:
    tenant = await _make_tenant(db, name="PANotCandidate")
    agent = await _make_agent(db, tenant_id=tenant, role="CLIENT_AGENT")
    await _make_agent(db, tenant_id=tenant, role="PLATFORM_ADMIN")
    await upsert_assignment_config(db, _claims(tenant), round_robin_enabled=True)

    for _ in range(3):
        chosen = await select_next_agent(db, _claims(tenant))
        assert chosen == agent


async def test_zero_active_agents_returns_none_ordinary_case(db: Database) -> None:
    tenant = await _make_tenant(db, name="Empty")
    await upsert_assignment_config(db, _claims(tenant), round_robin_enabled=True)

    result = await select_next_agent(db, _claims(tenant))

    assert result is None


async def test_deactivating_agent_mid_rotation_does_not_break_cursor(db: Database) -> None:
    tenant = await _make_tenant(db, name="Deactivate")
    agent_a = await _make_agent(db, tenant_id=tenant)
    agent_b = await _make_agent(db, tenant_id=tenant)
    await upsert_assignment_config(db, _claims(tenant), round_robin_enabled=True)

    ordered = sorted([agent_a, agent_b])
    first = await select_next_agent(db, _claims(tenant))
    assert first == ordered[0]

    # Deactivate the agent that was NOT just assigned.
    other = ordered[1] if first == ordered[0] else ordered[0]
    await db.execute("UPDATE users SET active = false WHERE id = $1", other)

    # Pool now has exactly one active agent; rotation must not error or
    # silently return None -- it keeps assigning the sole remaining agent.
    second = await select_next_agent(db, _claims(tenant))
    third = await select_next_agent(db, _claims(tenant))
    assert second == first
    assert third == first


# -- Concurrency (D4 -- the classic read-then-write race) -------------------


async def test_concurrent_assignments_distribute_without_duplication_bias(db: Database) -> None:
    """N simultaneous select_next_agent calls against a 2-agent pool must
    split roughly evenly -- proof against a real Postgres row lock, not a
    simulated sequence. A broken (non-atomic) implementation would show one
    agent receiving nearly all N assignments ("everyone gets agent A")."""
    tenant = await _make_tenant(db, name="Concurrent")
    agent_a = await _make_agent(db, tenant_id=tenant)
    agent_b = await _make_agent(db, tenant_id=tenant)
    await upsert_assignment_config(db, _claims(tenant), round_robin_enabled=True)

    claims = _claims(tenant)
    n = 20
    results = await asyncio.gather(*[select_next_agent(db, claims) for _ in range(n)])

    assert all(r in (agent_a, agent_b) for r in results)
    counts = Counter(results)
    # A correct atomic UPDATE...RETURNING alternates the cursor under
    # concurrent load; a broken read-then-write would produce a heavily
    # skewed split (e.g. 19/1) because most callers read the same stale
    # cursor before any of them wrote. Assert a roughly even split.
    assert min(counts.values()) >= n // 2 - 4, counts

    # The persisted cursor after N concurrent advances must be a real,
    # single value from the pool (not corrupted by a lost update).
    final = await db.fetchrow(
        "SELECT last_assigned_agent_id FROM tenant_assignment_configs WHERE tenant_id = $1",
        tenant,
    )
    assert final["last_assigned_agent_id"] in (agent_a, agent_b)


async def test_concurrent_cross_tenant_assignments_never_cross_contaminate(db: Database) -> None:
    """Concurrent assignment activity in tenant A must never affect tenant
    B's cursor or agent pool, even under simultaneous load on both."""
    tenant_a = await _make_tenant(db, name="ConcA")
    tenant_b = await _make_tenant(db, name="ConcB")
    agent_a = await _make_agent(db, tenant_id=tenant_a)
    agent_b = await _make_agent(db, tenant_id=tenant_b)
    await upsert_assignment_config(db, _claims(tenant_a), round_robin_enabled=True)
    await upsert_assignment_config(db, _claims(tenant_b), round_robin_enabled=True)

    claims_a, claims_b = _claims(tenant_a), _claims(tenant_b)
    results = await asyncio.gather(
        *[select_next_agent(db, claims_a) for _ in range(10)],
        *[select_next_agent(db, claims_b) for _ in range(10)],
    )

    assert all(r in (agent_a, agent_b) for r in results)
    a_results = results[:10]
    b_results = results[10:]
    assert all(r == agent_a for r in a_results)
    assert all(r == agent_b for r in b_results)
