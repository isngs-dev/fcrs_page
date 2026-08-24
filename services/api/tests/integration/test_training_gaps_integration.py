"""Real-Postgres proof for ``list_low_confidence_messages`` (Train the Agent's
"Coverage check" feed).

Mirrors ``test_crm_list_sort_integration.py``'s own rationale: the ``LATERAL``
join pairing a low-confidence bot turn with its preceding user question, and
the tenant predicate, are proven against ``TEST_DATABASE_URL`` only -- the
unit-test stub (``test_conversation_repository.py``) only proves the emitted
SQL's shape, never that the join/ordering is actually correct.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.conversation_store.repository import list_low_confidence_messages

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")

if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping Train the Agent Postgres proof", allow_module_level=True)

if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip(
        "TEST_DATABASE_URL equals a development URL — refusing destructive integration setup",
        allow_module_level=True,
    )


@pytest.fixture
async def db() -> Database:
    """Minimal tenants/conversations/messages schema mirroring migrations
    0002/0007/0008/0024."""
    assert _TEST_DB_URL is not None
    database = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0)
    await database.execute("DROP TABLE IF EXISTS messages CASCADE")
    await database.execute("DROP TABLE IF EXISTS conversations CASCADE")
    await database.execute("DROP TABLE IF EXISTS tenants CASCADE")
    await database.execute(
        """
        CREATE TABLE tenants (
            id text PRIMARY KEY,
            name text NOT NULL,
            slug text NOT NULL UNIQUE,
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE conversations (
            conversation_id text PRIMARY KEY,
            tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            visitor_id text,
            status text NOT NULL DEFAULT 'active',
            channel text NOT NULL DEFAULT 'widget',
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE messages (
            message_id text NOT NULL,
            tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            conversation_id text NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
            role text NOT NULL,
            content text NOT NULL,
            intent text,
            confidence double precision,
            tokens integer,
            decision text,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, conversation_id, message_id)
        )
        """
    )
    try:
        yield database
    finally:
        await database.execute("DROP TABLE IF EXISTS messages CASCADE")
        await database.execute("DROP TABLE IF EXISTS conversations CASCADE")
        await database.execute("DROP TABLE IF EXISTS tenants CASCADE")
        await database.close()


async def _seed_tenant(db: Database, tenant_id: str) -> None:
    await db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $1, $1) ON CONFLICT DO NOTHING",
        tenant_id,
    )


async def _seed_turn(
    db: Database,
    *,
    tenant_id: str,
    conversation_id: str,
    question: str,
    decision: str,
    confidence: float,
    at: datetime,
) -> None:
    """Insert a user turn immediately followed by a bot turn (5s later)."""
    await db.execute(
        "INSERT INTO conversations (conversation_id, tenant_id) VALUES ($1, $2) "
        "ON CONFLICT DO NOTHING",
        conversation_id,
        tenant_id,
    )
    user_id = uuid4().hex
    bot_id = uuid4().hex
    await db.execute(
        "INSERT INTO messages "
        "(message_id, tenant_id, conversation_id, role, content, created_at) "
        "VALUES ($1, $2, $3, 'user', $4, $5)",
        user_id, tenant_id, conversation_id, question, at,
    )
    await db.execute(
        "INSERT INTO messages "
        "(message_id, tenant_id, conversation_id, role, content, confidence, decision, created_at) "
        "VALUES ($1, $2, $3, 'bot', 'sorry, cannot help', $4, $5, $6)",
        bot_id, tenant_id, conversation_id, confidence, decision, at + timedelta(seconds=5),
    )


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


async def test_pairs_bot_turn_with_its_preceding_question(db: Database) -> None:
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    await _seed_tenant(db, tenant_id)
    await _seed_turn(
        db,
        tenant_id=tenant_id,
        conversation_id=uuid4().hex,
        question="How much does an inspection cost?",
        decision="escalate",
        confidence=0.1,
        at=datetime.now(UTC),
    )

    gaps = await list_low_confidence_messages(db, _claims(tenant_id))

    assert len(gaps) == 1
    assert gaps[0].question == "How much does an inspection cost?"
    assert gaps[0].decision == "escalate"


async def test_excludes_answered_turns(db: Database) -> None:
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    await _seed_tenant(db, tenant_id)
    await _seed_turn(
        db,
        tenant_id=tenant_id,
        conversation_id=uuid4().hex,
        question="What are your hours?",
        decision="answer",
        confidence=0.9,
        at=datetime.now(UTC),
    )

    gaps = await list_low_confidence_messages(db, _claims(tenant_id))

    assert gaps == []


async def test_orders_newest_first(db: Database) -> None:
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    await _seed_tenant(db, tenant_id)
    now = datetime.now(UTC)
    await _seed_turn(
        db, tenant_id=tenant_id, conversation_id=uuid4().hex, question="older question",
        decision="escalate", confidence=0.1, at=now - timedelta(hours=1),
    )
    await _seed_turn(
        db, tenant_id=tenant_id, conversation_id=uuid4().hex, question="newer question",
        decision="clarify", confidence=0.4, at=now,
    )

    gaps = await list_low_confidence_messages(db, _claims(tenant_id))

    assert [g.question for g in gaps] == ["newer question", "older question"]


async def test_tenant_isolation_mandatory(db: Database) -> None:
    """Tenant A must never see tenant B's coverage gaps."""
    tenant_a = f"tenant-a-{uuid4().hex[:8]}"
    tenant_b = f"tenant-b-{uuid4().hex[:8]}"
    await _seed_tenant(db, tenant_a)
    await _seed_tenant(db, tenant_b)
    await _seed_turn(
        db, tenant_id=tenant_a, conversation_id=uuid4().hex, question="tenant a question",
        decision="escalate", confidence=0.1, at=datetime.now(UTC),
    )
    await _seed_turn(
        db, tenant_id=tenant_b, conversation_id=uuid4().hex, question="tenant b question",
        decision="escalate", confidence=0.1, at=datetime.now(UTC),
    )

    gaps_a = await list_low_confidence_messages(db, _claims(tenant_a))

    assert len(gaps_a) == 1
    assert gaps_a[0].question == "tenant a question"
