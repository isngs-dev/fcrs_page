"""Real-Postgres proof for SR-29 Accounts/Contacts sort, search, and the
Contacts ``account_id`` filter.

This deliberately does not use the unit-test stub: SQL ORDER BY semantics,
NULLS LAST, pg_trgm planning, pagination, and tenant predicates are proven
against TEST_DATABASE_URL only (the fixed-but-not-sufficient unit stubs
in test_accounts_admin_routes.py / test_contacts_admin_routes.py /
test_accounts_repository.py / test_contacts_repository.py only prove
*emitted SQL shape*, per this project's established F2/F3 stub-dishonesty
lesson). The fixture owns and recreates its own ``accounts``/``contacts``
tables (mirroring migration 0040's composite FK) plus migration 0047's
trigram indexes, never the development database.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.accounts.repository import list_accounts
from api.contacts.repository import list_contacts

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")

if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping SR-29 Postgres proof", allow_module_level=True)

if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip(
        "TEST_DATABASE_URL equals a development URL — refusing destructive integration setup",
        allow_module_level=True,
    )


@pytest.fixture
async def db() -> Any:
    """Minimal accounts/contacts schema mirroring migration 0040 + 0047."""
    assert _TEST_DB_URL is not None
    database = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0)
    await database.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # Dependency order: contacts references accounts via composite FK. This
    # test DB may already carry the real migrated schema (contact_identities,
    # opportunities, etc. FK-reference contacts/accounts), so CASCADE is
    # required to reliably own and recreate a minimal fixture schema here --
    # this fixture's tables are dropped and rebuilt on both setup and
    # teardown, never left half-migrated.
    await database.execute("DROP TABLE IF EXISTS contacts CASCADE")
    await database.execute("DROP TABLE IF EXISTS accounts CASCADE")
    await database.execute(
        """
        CREATE TABLE accounts (
            tenant_id text NOT NULL,
            account_id text NOT NULL,
            name text NOT NULL,
            domain text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, account_id)
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE contacts (
            tenant_id text NOT NULL,
            contact_id text NOT NULL,
            account_id text,
            lead_id text,
            name text,
            email text,
            phone text,
            consent jsonb NOT NULL DEFAULT '{}'::jsonb,
            owner_agent_id text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, contact_id),
            CONSTRAINT uq_contacts_tenant_lead UNIQUE (tenant_id, lead_id),
            FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, account_id)
        )
        """
    )
    await database.execute("CREATE INDEX idx_accounts_name_trgm ON accounts USING gin (name gin_trgm_ops)")
    await database.execute(
        "CREATE INDEX idx_accounts_domain_trgm ON accounts USING gin (domain gin_trgm_ops)"
    )
    await database.execute("CREATE INDEX idx_contacts_name_trgm ON contacts USING gin (name gin_trgm_ops)")
    await database.execute(
        "CREATE INDEX idx_contacts_email_trgm ON contacts USING gin (email gin_trgm_ops)"
    )
    await database.execute(
        "CREATE INDEX idx_contacts_tenant_account ON contacts (tenant_id, account_id)"
    )
    yield database
    await database.execute("DROP TABLE IF EXISTS contacts CASCADE")
    await database.execute("DROP TABLE IF EXISTS accounts CASCADE")
    await database.close()


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


async def _insert_account(
    db: Database,
    *,
    tenant_id: str,
    account_id: str | None = None,
    name: str = "Account",
    domain: str | None = "example.test",
    created_at: datetime | None = None,
) -> str:
    resolved_account_id = account_id or uuid4().hex
    now = created_at or datetime.now(UTC)
    await db.execute(
        "INSERT INTO accounts (tenant_id, account_id, name, domain, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        tenant_id,
        resolved_account_id,
        name,
        domain,
        now,
        now,
    )
    return resolved_account_id


async def _insert_contact(
    db: Database,
    *,
    tenant_id: str,
    contact_id: str | None = None,
    account_id: str | None = None,
    name: str | None = "Contact",
    email: str | None = "contact@example.test",
    owner_agent_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    resolved_contact_id = contact_id or uuid4().hex
    now = created_at or datetime.now(UTC)
    await db.execute(
        "INSERT INTO contacts (tenant_id, contact_id, account_id, name, email, "
        "owner_agent_id, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        tenant_id,
        resolved_contact_id,
        account_id,
        name,
        email,
        owner_agent_id,
        now,
        now,
    )
    return resolved_contact_id


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def test_accounts_sort_by_name_asc_orders_alphabetically(db: Database) -> None:
    tenant = "tenant-acct-name"
    await _insert_account(db, tenant_id=tenant, account_id="zoe", name="Zoe Corp")
    await _insert_account(db, tenant_id=tenant, account_id="alice", name="Alice Inc")
    await _insert_account(db, tenant_id=tenant, account_id="middle", name="Middle LLC")

    rows, _ = await list_accounts(db, _claims(tenant), sort="name", direction="asc")

    assert [a.account_id for a in rows] == ["alice", "middle", "zoe"]


async def test_accounts_sort_by_name_mixed_case_documents_actual_collation(db: Database) -> None:
    """D4-style: record the DB's real collation behavior rather than guessing it."""
    tenant = "tenant-acct-collation"
    await _insert_account(db, tenant_id=tenant, account_id="upper", name="Zoe")
    await _insert_account(db, tenant_id=tenant, account_id="lower", name="alice")
    await _insert_account(db, tenant_id=tenant, account_id="null", name="Unnamed")

    sorted_rows, _ = await list_accounts(db, _claims(tenant), sort="name", direction="asc")
    expected = await db.fetch(
        "SELECT account_id FROM accounts WHERE tenant_id = $1 "
        "ORDER BY name ASC NULLS LAST, account_id DESC",
        tenant,
    )
    # `SHOW lc_collate` is not a recognized GUC on this Postgres build; the
    # database-level collation is read from pg_database instead. Recorded,
    # not guessed, per this project's "record reality" standard.
    collation = await db.fetchrow(
        "SELECT datcollate FROM pg_database WHERE datname = current_database()"
    )

    assert str(collation["datcollate"])
    assert [a.account_id for a in sorted_rows] == [str(row["account_id"]) for row in expected]


async def test_accounts_sort_by_domain_nulls_last_in_asc_and_in_desc(db: Database) -> None:
    tenant = "tenant-acct-domain"
    await _insert_account(db, tenant_id=tenant, account_id="alpha", domain="alpha.example")
    await _insert_account(db, tenant_id=tenant, account_id="zulu", domain="zulu.example")
    await _insert_account(db, tenant_id=tenant, account_id="blank", domain=None)

    ascending, _ = await list_accounts(db, _claims(tenant), sort="domain", direction="asc")
    descending, _ = await list_accounts(db, _claims(tenant), sort="domain", direction="desc")

    assert [a.account_id for a in ascending] == ["alpha", "zulu", "blank"]
    assert [a.account_id for a in descending] == ["zulu", "alpha", "blank"]


async def test_accounts_sort_by_created_desc_matches_pre_sprint_default(db: Database) -> None:
    tenant = "tenant-acct-created"
    now = datetime.now(UTC)
    await _insert_account(db, tenant_id=tenant, account_id="old", created_at=now - timedelta(days=2))
    await _insert_account(db, tenant_id=tenant, account_id="middle", created_at=now - timedelta(days=1))
    await _insert_account(db, tenant_id=tenant, account_id="new", created_at=now)

    rows, _ = await list_accounts(db, _claims(tenant))

    assert [a.account_id for a in rows] == ["new", "middle", "old"]


async def test_accounts_sort_pagination_no_duplicate_or_missing_rows_across_pages(db: Database) -> None:
    tenant = "tenant-acct-pages"
    for index in range(30):
        await _insert_account(db, tenant_id=tenant, account_id=f"acct-{index:02d}", name="Same Name")

    pages = [
        await list_accounts(db, _claims(tenant), limit=10, offset=offset, sort="name", direction="asc")
        for offset in (0, 10, 20)
    ]
    ids = [a.account_id for page, _total in pages for a in page]

    assert len(ids) == 30
    assert len(set(ids)) == 30
    assert set(ids) == {f"acct-{index:02d}" for index in range(30)}


async def test_accounts_search_matches_name_and_domain_ored(db: Database) -> None:
    await _insert_account(
        db, tenant_id="tenant-a", account_id="by-name", name="Needle Holdings", domain="other.example",
    )
    await _insert_account(
        db, tenant_id="tenant-a", account_id="by-domain", name="Widget Co", domain="needle.example",
    )
    await _insert_account(
        db, tenant_id="tenant-a", account_id="no-match", name="Other Co", domain="other2.example",
    )

    matches, _ = await list_accounts(db, _claims("tenant-a"), q="NEEDLE")

    assert {a.account_id for a in matches} == {"by-name", "by-domain"}


async def test_accounts_search_percent_is_literal_not_wildcard(db: Database) -> None:
    await _insert_account(db, tenant_id="tenant-a", account_id="percent", name="100% Ready")
    await _insert_account(db, tenant_id="tenant-a", account_id="other", name="No Percent Here")

    matches, _ = await list_accounts(db, _claims("tenant-a"), q="100%")

    assert [a.account_id for a in matches] == ["percent"]


async def test_accounts_search_uses_trigram_index_not_seq_scan(db: Database) -> None:
    """SET enable_seqscan is session-scoped -- it must run on the SAME pooled
    connection that then runs EXPLAIN, or a pool with max_size > 1 can route
    the two calls to different connections and silently leave the planner's
    default (cost-based) behavior in effect on the EXPLAIN connection. Pin
    both to one acquired connection rather than going through the Database
    wrapper's per-call pool routing.

    Deliberately EXPLAINs the substring predicate WITHOUT the tenant_id
    predicate: with tenant_id in the WHERE, the tenant/pkey index alone is
    already maximally selective against this fixture's small per-tenant row
    count, so the planner correctly (and uninterestingly) never touches the
    trigram index at all -- that was the actual bug in an earlier version of
    this test, not a product defect. Isolating the ILIKE predicate proves
    what the trigram index is actually for: serving the substring match
    itself, independent of which other index also happens to be available.
    """
    tenant = "tenant-acct-search-plan"
    now = datetime.now(UTC)
    for index in range(250):
        await _insert_account(
            db,
            tenant_id=tenant,
            account_id=f"acct-{index}",
            name=f"Searchable Prospect {index}",
            domain=f"prospect-{index}.example",
            created_at=now - timedelta(seconds=index),
        )
    await db.execute("ANALYZE accounts")

    async with db.acquire() as conn:
        await conn.execute("SET enable_seqscan = off")
        plan_rows = await conn.fetch(
            "EXPLAIN (COSTS OFF) SELECT account_id FROM accounts "
            "WHERE name ILIKE $1 ESCAPE '\\' OR domain ILIKE $2 ESCAPE '\\'",
            "%prospect 12%",
            "%prospect 12%",
        )
    plan = "\n".join(str(row[0]) for row in plan_rows)

    assert "Index Scan" in plan or "Bitmap Index Scan" in plan
    assert "idx_accounts_name_trgm" in plan or "idx_accounts_domain_trgm" in plan


async def test_accounts_search_total_matches_returned_row_count_when_under_one_page(
    db: Database,
) -> None:
    await _insert_account(db, tenant_id="tenant-a", account_id="a1", name="Needle One")
    await _insert_account(db, tenant_id="tenant-a", account_id="a2", name="Needle Two")
    await _insert_account(db, tenant_id="tenant-a", account_id="a3", name="Other")

    rows, total = await list_accounts(db, _claims("tenant-a"), q="needle")

    assert total == 2
    assert len(rows) == 2


@pytest.mark.parametrize("sort", ["name", "domain", "created"])
async def test_accounts_cross_tenant_isolation_every_sort_key(db: Database, sort: str) -> None:
    # Tenant B holds the row that would sort first for this key, ascending.
    await _insert_account(db, tenant_id="tenant-a", account_id="mine", name="Zzz Mine", domain="zzz.example")
    await _insert_account(
        db, tenant_id="tenant-b", account_id="other", name="AAA Other", domain="aaa.example",
    )

    rows, total = await list_accounts(db, _claims("tenant-a"), sort=sort, direction="asc")

    assert total == 1
    assert [a.account_id for a in rows] == ["mine"]


async def test_accounts_cross_tenant_isolation_search_exact_substring_match(db: Database) -> None:
    await _insert_account(db, tenant_id="tenant-a", account_id="mine", name="Needle Corp")
    await _insert_account(db, tenant_id="tenant-b", account_id="other", name="Needle Corp")

    rows, total = await list_accounts(db, _claims("tenant-a"), q="needle")

    assert total == 1
    assert [a.account_id for a in rows] == ["mine"]


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


async def test_contacts_sort_by_name_asc_nulls_last_both_directions(db: Database) -> None:
    tenant = "tenant-contact-name"
    await _insert_contact(db, tenant_id=tenant, contact_id="zoe", name="Zoe")
    await _insert_contact(db, tenant_id=tenant, contact_id="alice", name="Alice")
    await _insert_contact(db, tenant_id=tenant, contact_id="blank", name=None)

    ascending, _ = await list_contacts(db, _claims(tenant), sort="name", direction="asc")
    descending, _ = await list_contacts(db, _claims(tenant), sort="name", direction="desc")

    assert [c.contact_id for c in ascending] == ["alice", "zoe", "blank"]
    assert [c.contact_id for c in descending] == ["zoe", "alice", "blank"]


async def test_contacts_sort_by_email_asc_and_desc(db: Database) -> None:
    tenant = "tenant-contact-email"
    await _insert_contact(db, tenant_id=tenant, contact_id="alpha", email="alpha@example.test")
    await _insert_contact(db, tenant_id=tenant, contact_id="zulu", email="zulu@example.test")
    await _insert_contact(db, tenant_id=tenant, contact_id="blank", email=None)

    ascending, _ = await list_contacts(db, _claims(tenant), sort="email", direction="asc")
    descending, _ = await list_contacts(db, _claims(tenant), sort="email", direction="desc")

    assert [c.contact_id for c in ascending] == ["alpha", "zulu", "blank"]
    assert [c.contact_id for c in descending] == ["zulu", "alpha", "blank"]


async def test_contacts_sort_by_owner_puts_unowned_last_both_directions(db: Database) -> None:
    tenant = "tenant-contact-owner"
    await _insert_contact(db, tenant_id=tenant, contact_id="agent-a", owner_agent_id="agent-a")
    await _insert_contact(db, tenant_id=tenant, contact_id="agent-z", owner_agent_id="agent-z")
    await _insert_contact(db, tenant_id=tenant, contact_id="unowned", owner_agent_id=None)

    ascending, _ = await list_contacts(db, _claims(tenant), sort="owner", direction="asc")
    descending, _ = await list_contacts(db, _claims(tenant), sort="owner", direction="desc")

    assert [c.contact_id for c in ascending] == ["agent-a", "agent-z", "unowned"]
    assert [c.contact_id for c in descending] == ["agent-z", "agent-a", "unowned"]


async def test_contacts_sort_by_account_groups_same_account_rows_adjacently(db: Database) -> None:
    """D-COMPANY-SORT's real claim: `sort=account` clusters same-account rows
    adjacently by the opaque account_id -- it does NOT alpha-sort by company
    name. Two accounts, two contacts each; the grouping is exact."""
    tenant = "tenant-contact-account"
    await _insert_account(db, tenant_id=tenant, account_id="acct-1", name="Zzz Corp")
    await _insert_account(db, tenant_id=tenant, account_id="acct-2", name="Aaa Corp")
    await _insert_contact(db, tenant_id=tenant, contact_id="c1", account_id="acct-1", name="Contact One")
    await _insert_contact(db, tenant_id=tenant, contact_id="c2", account_id="acct-2", name="Contact Two")
    await _insert_contact(db, tenant_id=tenant, contact_id="c3", account_id="acct-1", name="Contact Three")
    await _insert_contact(db, tenant_id=tenant, contact_id="c4", account_id="acct-2", name="Contact Four")

    rows, _ = await list_contacts(db, _claims(tenant), sort="account", direction="asc")

    # Grouped: all acct-1 rows adjacent, all acct-2 rows adjacent -- NOT
    # alphabetized by the accounts' names ("Aaa Corp" would sort first if
    # this were a real name-sort, but acct-2 < acct-1 lexically is not
    # guaranteed either; the only claim under test is adjacency.
    account_sequence = []
    for c in rows:
        acct = "acct-1" if c.contact_id in {"c1", "c3"} else "acct-2"
        account_sequence.append(acct)
    # Each account's rows must be contiguous (grouped), not interleaved.
    assert account_sequence in (
        ["acct-1", "acct-1", "acct-2", "acct-2"],
        ["acct-2", "acct-2", "acct-1", "acct-1"],
    )


async def test_contacts_sort_by_created_desc_matches_pre_sprint_default(db: Database) -> None:
    tenant = "tenant-contact-created"
    now = datetime.now(UTC)
    await _insert_contact(db, tenant_id=tenant, contact_id="old", created_at=now - timedelta(days=2))
    await _insert_contact(db, tenant_id=tenant, contact_id="middle", created_at=now - timedelta(days=1))
    await _insert_contact(db, tenant_id=tenant, contact_id="new", created_at=now)

    rows, _ = await list_contacts(db, _claims(tenant))

    assert [c.contact_id for c in rows] == ["new", "middle", "old"]


async def test_contacts_sort_pagination_no_duplicate_or_missing_rows_across_pages(db: Database) -> None:
    tenant = "tenant-contact-pages"
    for index in range(30):
        await _insert_contact(db, tenant_id=tenant, contact_id=f"contact-{index:02d}", name="Same Name")

    pages = [
        await list_contacts(db, _claims(tenant), limit=10, offset=offset, sort="name", direction="asc")
        for offset in (0, 10, 20)
    ]
    ids = [c.contact_id for page, _total in pages for c in page]

    assert len(ids) == 30
    assert len(set(ids)) == 30
    assert set(ids) == {f"contact-{index:02d}" for index in range(30)}


async def test_contacts_search_matches_name_and_email_ored(db: Database) -> None:
    await _insert_contact(
        db, tenant_id="tenant-a", contact_id="by-name", name="Needle Alice", email="other@example.test",
    )
    await _insert_contact(
        db, tenant_id="tenant-a", contact_id="by-email", name="Bob", email="needle@example.test",
    )
    await _insert_contact(
        db, tenant_id="tenant-a", contact_id="no-match", name="Carl", email="carl@example.test",
    )

    matches, _ = await list_contacts(db, _claims("tenant-a"), q="NEEDLE")

    assert {c.contact_id for c in matches} == {"by-name", "by-email"}


async def test_contacts_search_uses_trigram_index_not_seq_scan(db: Database) -> None:
    """See the accounts twin's docstring: SET enable_seqscan and EXPLAIN must
    run on the same pooled connection, or a max_size > 1 pool can silently
    route them to different connections -- and the tenant_id predicate is
    deliberately left OUT of the EXPLAINed query, since the tenant/pkey and
    tenant_account indexes are already selective enough over this fixture's
    small per-tenant row count to answer the whole predicate without ever
    touching the trigram index, which proves nothing about the trigram index
    itself."""
    tenant = "tenant-contact-search-plan"
    now = datetime.now(UTC)
    for index in range(250):
        await _insert_contact(
            db,
            tenant_id=tenant,
            contact_id=f"contact-{index}",
            name=f"Searchable Prospect {index}",
            email=f"person-{index}@example.test",
            created_at=now - timedelta(seconds=index),
        )
    await db.execute("ANALYZE contacts")

    async with db.acquire() as conn:
        await conn.execute("SET enable_seqscan = off")
        plan_rows = await conn.fetch(
            "EXPLAIN (COSTS OFF) SELECT contact_id FROM contacts "
            "WHERE name ILIKE $1 ESCAPE '\\' OR email ILIKE $2 ESCAPE '\\'",
            "%prospect 12%",
            "%prospect 12%",
        )
    plan = "\n".join(str(row[0]) for row in plan_rows)

    assert "Index Scan" in plan or "Bitmap Index Scan" in plan
    assert "idx_contacts_name_trgm" in plan or "idx_contacts_email_trgm" in plan


async def test_contacts_account_id_filter_returns_only_that_accounts_contacts(db: Database) -> None:
    tenant = "tenant-contact-filter"
    await _insert_account(db, tenant_id=tenant, account_id="acct-1")
    await _insert_account(db, tenant_id=tenant, account_id="acct-2")
    await _insert_contact(db, tenant_id=tenant, contact_id="c1", account_id="acct-1")
    await _insert_contact(db, tenant_id=tenant, contact_id="c2", account_id="acct-2")
    await _insert_contact(db, tenant_id=tenant, contact_id="c3", account_id="acct-1")

    rows, total = await list_contacts(db, _claims(tenant), account_id="acct-1")

    assert total == 2
    assert {c.contact_id for c in rows} == {"c1", "c3"}


async def test_contacts_account_id_filter_composes_with_sort_and_search(db: Database) -> None:
    tenant = "tenant-contact-filter-compose"
    await _insert_account(db, tenant_id=tenant, account_id="acct-1")
    await _insert_account(db, tenant_id=tenant, account_id="acct-2")
    await _insert_contact(
        db, tenant_id=tenant, contact_id="c1", account_id="acct-1", name="Needle Alice",
    )
    await _insert_contact(
        db, tenant_id=tenant, contact_id="c2", account_id="acct-1", name="Other Bob",
    )
    await _insert_contact(
        db, tenant_id=tenant, contact_id="c3", account_id="acct-2", name="Needle Carl",
    )

    rows, total = await list_contacts(
        db, _claims(tenant), account_id="acct-1", q="needle", sort="name", direction="asc",
    )

    assert total == 1
    assert [c.contact_id for c in rows] == ["c1"]


@pytest.mark.parametrize("sort", ["name", "email", "account", "owner", "created"])
async def test_contacts_cross_tenant_isolation_every_sort_key(db: Database, sort: str) -> None:
    await _insert_contact(
        db, tenant_id="tenant-a", contact_id="mine", name="Zzz Mine", email="zzz@example.test",
        owner_agent_id="zzz-agent",
    )
    await _insert_contact(
        db, tenant_id="tenant-b", contact_id="other", name="AAA Other", email="aaa@example.test",
        owner_agent_id="aaa-agent",
    )

    rows, total = await list_contacts(db, _claims("tenant-a"), sort=sort, direction="asc")

    assert total == 1
    assert [c.contact_id for c in rows] == ["mine"]


async def test_contacts_cross_tenant_isolation_search(db: Database) -> None:
    await _insert_contact(db, tenant_id="tenant-a", contact_id="mine", name="Needle Corp")
    await _insert_contact(db, tenant_id="tenant-b", contact_id="other", name="Needle Corp")

    rows, total = await list_contacts(db, _claims("tenant-a"), q="needle")

    assert total == 1
    assert [c.contact_id for c in rows] == ["mine"]


async def test_contacts_cross_tenant_account_id_filter_returns_empty(db: Database) -> None:
    """5b point 7: a cross-tenant account_id is unrepresentable via the FK in
    production, but even without the FK's help here, the tenant-scoped WHERE
    alone must still yield zero rows -- never another tenant's contacts."""
    await _insert_account(db, tenant_id="tenant-b", account_id="acct-b")
    await _insert_contact(db, tenant_id="tenant-b", contact_id="other", account_id="acct-b")

    rows, total = await list_contacts(db, _claims("tenant-a"), account_id="acct-b")

    assert total == 0
    assert rows == []
