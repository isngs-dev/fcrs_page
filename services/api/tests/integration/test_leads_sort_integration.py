"""Real-Postgres proof for SR-25 lead sort and Name/Email search.

This deliberately does not use the unit-test stub: SQL ORDER BY semantics,
NULLS LAST, pg_trgm planning, pagination, and tenant predicates are proven
against TEST_DATABASE_URL only. The fixture owns and recreates its ``leads``
table, never the development database.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.leads.repository import list_leads

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")

if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping SR-25 Postgres proof", allow_module_level=True)

if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip(
        "TEST_DATABASE_URL equals a development URL — refusing destructive integration setup",
        allow_module_level=True,
    )


@pytest.fixture
async def db() -> Any:
    """Minimal nullable-name/email schema from migrations 0014 + 0039 + 0046."""
    assert _TEST_DB_URL is not None
    database = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0)
    await database.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.execute(
        """
        CREATE TABLE leads (
            tenant_id text NOT NULL,
            lead_id text NOT NULL,
            visitor_id text,
            name text,
            email text,
            phone text,
            status text NOT NULL DEFAULT 'new',
            stage text NOT NULL DEFAULT 'captured',
            qualification_score integer,
            consent jsonb NOT NULL DEFAULT '{}'::jsonb,
            assigned_agent_id text,
            source text NOT NULL DEFAULT 'widget',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            converted_to_contact_id text,
            PRIMARY KEY (tenant_id, lead_id)
        )
        """
    )
    await database.execute("CREATE INDEX idx_leads_name_trgm ON leads USING gin (name gin_trgm_ops)")
    await database.execute("CREATE INDEX idx_leads_email_trgm ON leads USING gin (email gin_trgm_ops)")
    yield database
    await database.execute("DROP TABLE IF EXISTS leads")
    await database.close()


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


async def _insert_lead(
    db: Database,
    *,
    tenant_id: str,
    lead_id: str | None = None,
    name: str | None = "Lead",
    email: str | None = "lead@example.com",
    status: str = "new",
    stage: str = "captured",
    score: int | None = None,
    assigned_agent_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    resolved_lead_id = lead_id or uuid4().hex
    now = created_at or datetime.now(UTC)
    await db.execute(
        "INSERT INTO leads (tenant_id, lead_id, name, email, status, stage, "
        "qualification_score, assigned_agent_id, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        tenant_id,
        resolved_lead_id,
        name,
        email,
        status,
        stage,
        score,
        assigned_agent_id,
        now,
        now,
    )
    return resolved_lead_id


async def test_score_sort_nulls_last_in_both_directions(db: Database) -> None:
    tenant = "tenant-score"
    await _insert_lead(db, tenant_id=tenant, lead_id="low", score=10)
    await _insert_lead(db, tenant_id=tenant, lead_id="high", score=90)
    await _insert_lead(db, tenant_id=tenant, lead_id="blank", score=None)

    descending, _ = await list_leads(db, _claims(tenant), sort="score", direction="desc")
    ascending, _ = await list_leads(db, _claims(tenant), sort="score", direction="asc")

    assert [lead.lead_id for lead in descending] == ["high", "low", "blank"]
    assert [lead.lead_id for lead in ascending] == ["low", "high", "blank"]


async def test_stage_and_status_sort_follow_domain_order_not_alphabetical(db: Database) -> None:
    tenant = "tenant-pipeline"
    await _insert_lead(db, tenant_id=tenant, lead_id="captured", stage="captured", status="new")
    await _insert_lead(db, tenant_id=tenant, lead_id="qualified", stage="qualified", status="open")
    await _insert_lead(db, tenant_id=tenant, lead_id="converted", stage="converted", status="won")
    await _insert_lead(db, tenant_id=tenant, lead_id="disqualified", stage="disqualified", status="lost")
    await _insert_lead(db, tenant_id=tenant, lead_id="future", stage="future", status="future")

    by_stage, _ = await list_leads(db, _claims(tenant), sort="stage", direction="asc")
    by_status, _ = await list_leads(db, _claims(tenant), sort="status", direction="asc")
    by_stage_desc, _ = await list_leads(db, _claims(tenant), sort="stage", direction="desc")
    by_status_desc, _ = await list_leads(db, _claims(tenant), sort="status", direction="desc")

    assert [lead.lead_id for lead in by_stage] == [
        "captured", "qualified", "converted", "disqualified", "future",
    ]
    assert [lead.lead_id for lead in by_status] == [
        "captured", "qualified", "converted", "disqualified", "future",
    ]
    assert [lead.lead_id for lead in by_stage_desc] == [
        "disqualified", "converted", "qualified", "captured", "future",
    ]
    assert [lead.lead_id for lead in by_status_desc] == [
        "disqualified", "converted", "qualified", "captured", "future",
    ]


async def test_name_sort_nulls_last_and_records_database_collation(db: Database) -> None:
    """D4: expected mixed-case order is read from the test DB's own collation."""
    tenant = "tenant-names"
    await _insert_lead(db, tenant_id=tenant, lead_id="upper", name="Zoe")
    await _insert_lead(db, tenant_id=tenant, lead_id="lower", name="alice")
    await _insert_lead(db, tenant_id=tenant, lead_id="null", name=None)

    sorted_rows, _ = await list_leads(db, _claims(tenant), sort="name", direction="asc")
    expected = await db.fetch(
        "SELECT lead_id FROM leads WHERE tenant_id = $1 "
        "ORDER BY name ASC NULLS LAST, lead_id DESC",
        tenant,
    )
    collation = await db.fetchrow("SHOW lc_collate")

    assert str(collation["lc_collate"])
    assert [lead.lead_id for lead in sorted_rows] == [str(row["lead_id"]) for row in expected]
    assert sorted_rows[-1].lead_id == "null"


async def test_email_sort_orders_ascending_and_descending_against_real_postgres(
    db: Database,
) -> None:
    tenant = "tenant-emails"
    await _insert_lead(db, tenant_id=tenant, lead_id="alpha", email="alpha@example.test")
    await _insert_lead(db, tenant_id=tenant, lead_id="middle", email="middle@example.test")
    await _insert_lead(db, tenant_id=tenant, lead_id="zulu", email="zulu@example.test")
    await _insert_lead(db, tenant_id=tenant, lead_id="blank", email=None)

    ascending, _ = await list_leads(db, _claims(tenant), sort="email", direction="asc")
    descending, _ = await list_leads(db, _claims(tenant), sort="email", direction="desc")

    assert [lead.lead_id for lead in ascending] == ["alpha", "middle", "zulu", "blank"]
    assert [lead.lead_id for lead in descending] == ["zulu", "middle", "alpha", "blank"]


async def test_assigned_sort_keeps_unassigned_last_in_both_directions(db: Database) -> None:
    tenant = "tenant-assigned"
    await _insert_lead(db, tenant_id=tenant, lead_id="agent-a", assigned_agent_id="agent-a")
    await _insert_lead(db, tenant_id=tenant, lead_id="agent-z", assigned_agent_id="agent-z")
    await _insert_lead(db, tenant_id=tenant, lead_id="unassigned", assigned_agent_id=None)

    ascending, _ = await list_leads(db, _claims(tenant), sort="assigned", direction="asc")
    descending, _ = await list_leads(db, _claims(tenant), sort="assigned", direction="desc")

    assert [lead.lead_id for lead in ascending] == ["agent-a", "agent-z", "unassigned"]
    assert [lead.lead_id for lead in descending] == ["agent-z", "agent-a", "unassigned"]


async def test_default_created_sort_remains_newest_first(db: Database) -> None:
    tenant = "tenant-created"
    now = datetime.now(UTC)
    await _insert_lead(db, tenant_id=tenant, lead_id="old", created_at=now - timedelta(days=2))
    await _insert_lead(db, tenant_id=tenant, lead_id="middle", created_at=now - timedelta(days=1))
    await _insert_lead(db, tenant_id=tenant, lead_id="new", created_at=now)

    rows, _ = await list_leads(db, _claims(tenant))

    assert [lead.lead_id for lead in rows] == ["new", "middle", "old"]


async def test_pagination_has_no_duplicate_or_missing_rows_for_tied_sort_values(db: Database) -> None:
    tenant = "tenant-pages"
    for index in range(30):
        await _insert_lead(db, tenant_id=tenant, lead_id=f"lead-{index:02d}", stage="captured")

    pages = [
        await list_leads(db, _claims(tenant), limit=10, offset=offset, sort="stage", direction="asc")
        for offset in (0, 10, 20)
    ]
    ids = [lead.lead_id for page, _total in pages for lead in page]

    assert len(ids) == 30
    assert len(set(ids)) == 30
    assert set(ids) == {f"lead-{index:02d}" for index in range(30)}


@pytest.mark.parametrize("sort", ["name", "email", "stage", "status", "score", "assigned", "created"])
async def test_every_sort_stays_tenant_scoped_real_postgres(db: Database, sort: str) -> None:
    await _insert_lead(
        db, tenant_id="tenant-a", lead_id="mine", name="Zoe", email="z@example.test",
        status="lost", stage="disqualified", score=0, assigned_agent_id="z-agent",
    )
    await _insert_lead(
        db, tenant_id="tenant-b", lead_id="other", name="Aaron", email="a@example.test",
        status="new", stage="captured", score=100, assigned_agent_id="a-agent",
    )

    rows, total = await list_leads(db, _claims("tenant-a"), sort=sort, direction="asc")

    assert total == 1
    assert [lead.lead_id for lead in rows] == ["mine"]


async def test_search_matches_name_or_email_escapes_wildcards_and_stays_tenant_scoped(db: Database) -> None:
    await _insert_lead(db, tenant_id="tenant-a", lead_id="name", name="Alice Needle")
    await _insert_lead(db, tenant_id="tenant-a", lead_id="email", email="needle@example.test")
    await _insert_lead(db, tenant_id="tenant-a", lead_id="percent", name="100% Ready")
    await _insert_lead(db, tenant_id="tenant-b", lead_id="other", name="Needle Other")

    matches, _ = await list_leads(db, _claims("tenant-a"), q="NEEDLE", sort="name", direction="asc")
    literal_percent, _ = await list_leads(db, _claims("tenant-a"), q="100%")

    assert {lead.lead_id for lead in matches} == {"name", "email"}
    assert [lead.lead_id for lead in literal_percent] == ["percent"]


async def test_search_uses_a_trigram_index(db: Database) -> None:
    tenant = "tenant-search-plan"
    now = datetime.now(UTC)
    for index in range(250):
        await _insert_lead(
            db,
            tenant_id=tenant,
            lead_id=f"lead-{index}",
            name=f"Searchable Prospect {index}",
            email=f"person-{index}@example.test",
            created_at=now - timedelta(seconds=index),
        )
    await db.execute("ANALYZE leads")
    # The assertion is intentionally an index-use proof rather than a
    # fragile cost estimate. Disabling seq scan makes this deterministic on
    # tiny CI databases while still requiring a usable trigram index.
    await db.execute("SET enable_seqscan = off")
    plan_rows = await db.fetch(
        "EXPLAIN (COSTS OFF) SELECT lead_id FROM leads "
        "WHERE tenant_id = $1 AND (name ILIKE $2 ESCAPE '\\' OR email ILIKE $3 ESCAPE '\\')",
        tenant,
        "%prospect 12%",
        "%prospect 12%",
    )
    plan = "\n".join(str(row[0]) for row in plan_rows)

    assert "Index Scan" in plan or "Bitmap Index Scan" in plan
    assert "idx_leads_name_trgm" in plan or "idx_leads_email_trgm" in plan
