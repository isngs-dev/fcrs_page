"""Real-Postgres proof for the Knowledge Base list feature's ``list_docs``.

This deliberately does not use the unit-test stub: real ``ORDER BY
created_at DESC`` semantics and the tenant predicate are proven against
``TEST_DATABASE_URL`` only, per this project's established F2/F3
stub-dishonesty lesson (see test_crm_list_sort_integration.py's own
docstring) -- the unit-test stubs in test_ingestion_repository.py /
test_ingestion_routes.py only prove emitted SQL shape and a synthetic
insertion-order proxy, not real Postgres ordering. The fixture owns and
recreates its own ``knowledge_docs`` table (mirroring migration 0010 +
0054), never the development database.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.ingestion.repository import list_docs

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")

if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping knowledge-docs-list Postgres proof", allow_module_level=True)

if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip(
        "TEST_DATABASE_URL equals a development URL — refusing destructive integration setup",
        allow_module_level=True,
    )


@pytest.fixture
async def db() -> Any:
    """Minimal knowledge_docs schema mirroring migration 0010 + 0054."""
    assert _TEST_DB_URL is not None
    database = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0)
    await database.execute("DROP TABLE IF EXISTS knowledge_docs CASCADE")
    await database.execute(
        """
        CREATE TABLE knowledge_docs (
            tenant_id text NOT NULL,
            doc_id text NOT NULL,
            source text NOT NULL,
            filename text NOT NULL,
            content_type text NOT NULL,
            status text NOT NULL,
            content_hash text NOT NULL,
            storage_key text NOT NULL,
            title text,
            description text,
            uploaded_by text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, doc_id),
            UNIQUE (tenant_id, content_hash)
        )
        """
    )
    await database.execute(
        "CREATE INDEX idx_knowledge_docs_tenant_created ON knowledge_docs (tenant_id, created_at)"
    )
    yield database
    await database.execute("DROP TABLE IF EXISTS knowledge_docs CASCADE")
    await database.close()


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


async def _insert_doc(
    db: Database,
    *,
    tenant_id: str,
    doc_id: str | None = None,
    filename: str = "doc.txt",
    title: str | None = None,
    uploaded_by: str | None = None,
    created_at: datetime | None = None,
) -> str:
    resolved_doc_id = doc_id or uuid4().hex
    now = created_at or datetime.now(UTC)
    await db.execute(
        "INSERT INTO knowledge_docs "
        "(tenant_id, doc_id, source, filename, content_type, status, content_hash, "
        " storage_key, title, uploaded_by, created_at, updated_at) "
        "VALUES ($1, $2, 'upload', $3, 'text/plain', 'pending', $4, $5, $6, $7, $8, $8)",
        tenant_id,
        resolved_doc_id,
        filename,
        f"hash-{resolved_doc_id}",
        f"{tenant_id}/{resolved_doc_id}/{filename}",
        title,
        uploaded_by,
        now,
    )
    return resolved_doc_id


async def test_list_docs_orders_by_created_at_desc_for_real(db: Database) -> None:
    tenant = "tenant-list-order"
    now = datetime.now(UTC)
    await _insert_doc(db, tenant_id=tenant, doc_id="old", created_at=now - timedelta(days=2))
    await _insert_doc(db, tenant_id=tenant, doc_id="newest", created_at=now)
    await _insert_doc(db, tenant_id=tenant, doc_id="middle", created_at=now - timedelta(days=1))

    docs = await list_docs(db, _claims(tenant))

    assert [d.doc_id for d in docs] == ["newest", "middle", "old"]


async def test_list_docs_tenant_isolation_for_real(db: Database) -> None:
    await _insert_doc(db, tenant_id="tenant-a", doc_id="mine", title="Mine")
    await _insert_doc(db, tenant_id="tenant-b", doc_id="other", title="Other")

    docs = await list_docs(db, _claims("tenant-a"))

    assert [d.doc_id for d in docs] == ["mine"]


async def test_list_docs_round_trips_title_description_uploaded_by(db: Database) -> None:
    tenant = "tenant-list-fields"
    await _insert_doc(
        db, tenant_id=tenant, doc_id="doc1", filename="raw.txt", title="Refund policy",
        uploaded_by="user-42",
    )

    docs = await list_docs(db, _claims(tenant))

    assert len(docs) == 1
    assert docs[0].title == "Refund policy"
    assert docs[0].filename == "raw.txt"
    assert docs[0].uploaded_by == "user-42"
    assert docs[0].description is None


async def test_list_docs_empty_tenant_returns_empty_list_for_real(db: Database) -> None:
    docs = await list_docs(db, _claims("tenant-empty"))

    assert docs == []
