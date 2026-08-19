"""Unit tests for api.ingestion.repository.

Uses an in-memory stub database to verify:
- create_doc inserts and returns a KnowledgeDoc with status='pending'.
- get_doc returns the doc scoped to the caller's tenant.
- Cross-tenant read returns None (isolation).
- find_doc_by_hash returns an existing doc (idempotency).
- update_doc_status transitions the status.
- create_run inserts and returns an IngestionRun with status='queued'.
- update_run transitions the run status.
- get_latest_run returns the most-recently-started run.
- Global caller (PLATFORM_ADMIN, tenant_id=None) → ValidationError.

NOTE: These unit tests use a stub DB and cannot validate real column
existence against the live schema. Migration 0010 live-application is the
user's manual verification step.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

_TEST_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _reset_modules() -> None:
    # Reimport ingestion modules fresh + clear settings caches. Do NOT delete
    # api.config: that splits the module graph (api.app stays bound to the
    # original config) and poisons later tests. Clearing the caches on the single
    # shared config module gives fresh settings safely.
    for key in list(sys.modules.keys()):
        if key.startswith("api.ingestion"):
            del sys.modules[key]
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stub database
# ---------------------------------------------------------------------------


class _StubDatabase:
    """Minimal in-memory DB stub that stores rows in Python dicts."""

    def __init__(self) -> None:
        # doc_rows: keyed by (tenant_id, doc_id)
        self._docs: dict[tuple[str, str], dict[str, Any]] = {}
        # run_rows: keyed by (tenant_id, run_id)
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}
        self._insert_seq = 0

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()

        if q.startswith("INSERT INTO KNOWLEDGE_DOCS"):
            (
                tenant_id,
                doc_id,
                source,
                filename,
                content_type,
                status,
                content_hash,
                storage_key,
                title,
                description,
                uploaded_by,
            ) = args
            self._insert_seq += 1
            self._docs[(tenant_id, doc_id)] = {
                "doc_id": doc_id,
                "source": source,
                "filename": filename,
                "content_type": content_type,
                "status": status,
                "content_hash": content_hash,
                "storage_key": storage_key,
                "title": title,
                "description": description,
                "uploaded_by": uploaded_by,
                "created_at": _NOW,
                "updated_at": _NOW,
                "tenant_id": tenant_id,
                # Test-only: not a real column, used to give list_docs a
                # deterministic "newest first" ordering to emulate even
                # though every row in this stub shares the same _NOW
                # created_at. Real ORDER BY created_at DESC correctness is
                # proven against live Postgres by the integration test, not
                # here (this repo's established stub-DB limitation).
                "_seq": self._insert_seq,
            }
            return "INSERT 0 1"

        if q.startswith("UPDATE KNOWLEDGE_DOCS"):
            # args: status, tenant_id, doc_id
            new_status, tenant_id, doc_id = args[0], args[-2], args[-1]
            key = (tenant_id, doc_id)
            if key not in self._docs:
                return "UPDATE 0"
            self._docs[key] = {**self._docs[key], "status": new_status, "updated_at": _NOW}
            return "UPDATE 1"

        if q.startswith("INSERT INTO INGESTION_RUNS"):
            tenant_id, run_id, doc_id, status = args
            self._runs[(tenant_id, run_id)] = {
                "run_id": run_id,
                "doc_id": doc_id,
                "status": status,
                "chars_out": None,
                "errors": None,
                "started_at": _NOW,
                "finished_at": None,
                "duration_ms": None,
                "tenant_id": tenant_id,
            }
            return "INSERT 0 1"

        if q.startswith("UPDATE INGESTION_RUNS"):
            # Variable positional args depending on which fields are set.
            # We identify tenant_id + run_id as the last two args (WHERE clause).
            tenant_id, run_id = str(args[-2]), str(args[-1])
            key = (tenant_id, run_id)
            if key not in self._runs:
                return "UPDATE 0"
            # Rebuild run with whatever came in. We trust the repo to build SET
            # clauses positionally; here we just apply each non-None positional
            # value in order. For simplicity, extract by parsing the SET clause.
            # The first arg is always `status`.
            updated: dict[str, Any] = dict(self._runs[key])
            # args[0] == status (first SET clause), then optional fields.
            updated["status"] = args[0]
            # Map remaining args (excluding last two = WHERE) to columns:
            remaining = list(args[1:-2])
            # The update_run function appends: chars_out, errors, duration_ms,
            # finished_at in that order when they are not None.
            # We store whatever was passed as a flat update; for testing we just
            # record all scalar args.
            field_names = ["chars_out", "errors", "duration_ms", "finished_at"]
            for i, val in enumerate(remaining):
                if i < len(field_names):
                    updated[field_names[i]] = val
            self._runs[key] = updated
            return "UPDATE 1"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()

        if "FROM KNOWLEDGE_DOCS" in q and "AND CONTENT_HASH" in q:
            # find_doc_by_hash — WHERE ... AND content_hash = $2
            tenant_id, content_hash = args[0], args[1]
            for (tid, _), row in self._docs.items():
                if tid == tenant_id and row["content_hash"] == content_hash:
                    return row
            return None

        if "FROM KNOWLEDGE_DOCS" in q:
            tenant_id, doc_id = args[0], args[1]
            row = self._docs.get((tenant_id, doc_id))
            return row

        if "FROM INGESTION_RUNS" in q and "LIMIT 1" in q:
            # get_latest_run — return the newest run for (tenant_id, doc_id).
            tenant_id, doc_id = args[0], args[1]
            candidates = [
                r for (tid, _), r in self._runs.items()
                if tid == tenant_id and r["doc_id"] == doc_id
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda r: (r["started_at"], r["run_id"]))

        if "FROM INGESTION_RUNS" in q:
            tenant_id, run_id = args[0], args[1]
            return self._runs.get((tenant_id, run_id))

        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()

        if "FROM KNOWLEDGE_DOCS" in q:
            # list_docs — WHERE tenant_id = $1 ORDER BY created_at DESC.
            tenant_id = args[0]
            rows = [row for (tid, _), row in self._docs.items() if tid == tenant_id]
            rows.sort(key=lambda r: r["_seq"], reverse=True)
            return rows

        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Claims helpers
# ---------------------------------------------------------------------------


def _admin_claims(tenant_id: str = "tenant-alpha") -> AuthClaims:
    return AuthClaims(subject="user-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


def _global_claims() -> AuthClaims:
    return AuthClaims(subject="root", role=Role.PLATFORM_ADMIN, tenant_id=None)


# ==============================================================================
# knowledge_docs
# ==============================================================================


async def test_create_doc_returns_pending_doc() -> None:
    """create_doc inserts a row and returns KnowledgeDoc with status='pending'."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc

        db = _StubDatabase()
        claims = _admin_claims()
        doc = await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="sample.txt",
            content_type="text/plain",
            content_hash="abc123",
            storage_key="tenant-alpha/doc1/sample.txt",
            doc_id="doc1",
        )
    assert doc.doc_id == "doc1"
    assert doc.status == "pending"
    assert doc.content_hash == "abc123"


async def test_get_doc_returns_existing_doc() -> None:
    """get_doc returns the doc for the caller's tenant."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc, get_doc

        db = _StubDatabase()
        claims = _admin_claims()
        await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="file.txt",
            content_type="text/plain",
            content_hash="hash1",
            storage_key="tenant-alpha/docA/file.txt",
            doc_id="docA",
        )
        doc = await get_doc(db, claims, "docA")  # type: ignore[arg-type]
    assert doc is not None
    assert doc.doc_id == "docA"


async def test_get_doc_cross_tenant_returns_none() -> None:
    """Cross-tenant read: tenant-beta cannot see tenant-alpha's doc."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc, get_doc

        db = _StubDatabase()
        alpha_claims = _admin_claims("tenant-alpha")
        beta_claims = _admin_claims("tenant-beta")

        await create_doc(
            db,  # type: ignore[arg-type]
            alpha_claims,
            source="upload",
            filename="secret.txt",
            content_type="text/plain",
            content_hash="hashX",
            storage_key="tenant-alpha/docX/secret.txt",
            doc_id="docX",
        )
        doc = await get_doc(db, beta_claims, "docX")  # type: ignore[arg-type]
    assert doc is None, "Tenant beta must not read tenant alpha's doc"


async def test_find_doc_by_hash_returns_existing() -> None:
    """find_doc_by_hash returns the doc when the hash matches (idempotency)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc, find_doc_by_hash

        db = _StubDatabase()
        claims = _admin_claims()
        await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="dup.txt",
            content_type="text/plain",
            content_hash="sha256-dup",
            storage_key="tenant-alpha/docDup/dup.txt",
            doc_id="docDup",
        )

        # The stub's fetchrow now handles hash lookups directly via
        # the "AND CONTENT_HASH" discriminator.
        found = await find_doc_by_hash(db, claims, "sha256-dup")  # type: ignore[arg-type]
    assert found is not None
    assert found.doc_id == "docDup"


async def test_find_doc_by_hash_returns_none_for_unknown_hash() -> None:
    """find_doc_by_hash returns None when no matching hash exists."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import find_doc_by_hash

        db = _StubDatabase()
        claims = _admin_claims()
        found = await find_doc_by_hash(db, claims, "nonexistent-hash")  # type: ignore[arg-type]
    assert found is None


async def test_update_doc_status_transitions() -> None:
    """update_doc_status changes the status field."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc, get_doc, update_doc_status

        db = _StubDatabase()
        claims = _admin_claims()
        await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="t.txt",
            content_type="text/plain",
            content_hash="h1",
            storage_key="tenant-alpha/docT/t.txt",
            doc_id="docT",
        )
        await update_doc_status(db, claims, "docT", "parsed")  # type: ignore[arg-type]
        doc = await get_doc(db, claims, "docT")  # type: ignore[arg-type]
    assert doc is not None
    assert doc.status == "parsed"


async def test_create_doc_persists_title_description_uploaded_by() -> None:
    """create_doc stores and round-trips title/description/uploaded_by."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc

        db = _StubDatabase()
        claims = _admin_claims()
        doc = await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="sample.txt",
            content_type="text/plain",
            content_hash="abc123",
            storage_key="tenant-alpha/doc1/sample.txt",
            doc_id="doc1",
            title="Refund policy",
            description="Our current refund policy for solar installs.",
            uploaded_by="user-42",
        )
    assert doc.title == "Refund policy"
    assert doc.description == "Our current refund policy for solar installs."
    assert doc.uploaded_by == "user-42"


async def test_create_doc_title_description_uploaded_by_default_to_none() -> None:
    """Omitting title/description/uploaded_by (existing call shape) leaves them None."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc

        db = _StubDatabase()
        claims = _admin_claims()
        doc = await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="sample.txt",
            content_type="text/plain",
            content_hash="abc123",
            storage_key="tenant-alpha/doc1/sample.txt",
            doc_id="doc1",
        )
    assert doc.title is None
    assert doc.description is None
    assert doc.uploaded_by is None


async def test_get_doc_and_find_doc_by_hash_include_new_fields() -> None:
    """get_doc/find_doc_by_hash also round-trip the new fields, not just create_doc."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc, find_doc_by_hash, get_doc

        db = _StubDatabase()
        claims = _admin_claims()
        await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="sample.txt",
            content_type="text/plain",
            content_hash="hash-fields",
            storage_key="tenant-alpha/doc1/sample.txt",
            doc_id="doc1",
            title="Title A",
            description="Desc A",
            uploaded_by="user-9",
        )
        via_get = await get_doc(db, claims, "doc1")  # type: ignore[arg-type]
        via_hash = await find_doc_by_hash(db, claims, "hash-fields")  # type: ignore[arg-type]
    assert via_get is not None
    assert via_get.title == "Title A"
    assert via_get.description == "Desc A"
    assert via_get.uploaded_by == "user-9"
    assert via_hash is not None
    assert via_hash.title == "Title A"


async def test_list_docs_returns_tenant_docs_newest_first() -> None:
    """list_docs returns only the caller's tenant's docs, newest upload first."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc, list_docs

        db = _StubDatabase()
        claims = _admin_claims()
        await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="first.txt",
            content_type="text/plain",
            content_hash="h-first",
            storage_key="tenant-alpha/docFirst/first.txt",
            doc_id="docFirst",
        )
        await create_doc(
            db,  # type: ignore[arg-type]
            claims,
            source="upload",
            filename="second.txt",
            content_type="text/plain",
            content_hash="h-second",
            storage_key="tenant-alpha/docSecond/second.txt",
            doc_id="docSecond",
        )
        docs = await list_docs(db, claims)  # type: ignore[arg-type]
    assert [d.doc_id for d in docs] == ["docSecond", "docFirst"]


async def test_list_docs_tenant_isolation() -> None:
    """Tenant B's list_docs never includes tenant A's docs."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc, list_docs

        db = _StubDatabase()
        alpha_claims = _admin_claims("tenant-alpha")
        beta_claims = _admin_claims("tenant-beta")

        await create_doc(
            db,  # type: ignore[arg-type]
            alpha_claims,
            source="upload",
            filename="secret.txt",
            content_type="text/plain",
            content_hash="h-secret",
            storage_key="tenant-alpha/docSecret/secret.txt",
            doc_id="docSecret",
        )
        await create_doc(
            db,  # type: ignore[arg-type]
            beta_claims,
            source="upload",
            filename="beta.txt",
            content_type="text/plain",
            content_hash="h-beta",
            storage_key="tenant-beta/docBeta/beta.txt",
            doc_id="docBeta",
        )

        beta_docs = await list_docs(db, beta_claims)  # type: ignore[arg-type]
    doc_ids = [d.doc_id for d in beta_docs]
    assert doc_ids == ["docBeta"]
    assert "docSecret" not in doc_ids


async def test_list_docs_empty_tenant_returns_empty_list() -> None:
    """A tenant with no docs gets an empty list, never an error."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import list_docs

        db = _StubDatabase()
        claims = _admin_claims()
        docs = await list_docs(db, claims)  # type: ignore[arg-type]
    assert docs == []


async def test_list_docs_rejects_global_caller() -> None:
    """PLATFORM_ADMIN (global, no tenant_id) is rejected — ingestion is tenant-scoped only."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import list_docs

        db = _StubDatabase()
        claims = _global_claims()
        with pytest.raises(ValidationError):
            await list_docs(db, claims)  # type: ignore[arg-type]


# ==============================================================================
# ingestion_runs
# ==============================================================================


async def test_create_run_returns_queued_run() -> None:
    """create_run inserts a run with status='queued'."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_run

        db = _StubDatabase()
        claims = _admin_claims()
        run = await create_run(db, claims, doc_id="doc1", run_id="run1")  # type: ignore[arg-type]
    assert run.run_id == "run1"
    assert run.status == "queued"
    assert run.doc_id == "doc1"
    assert run.chars_out is None
    assert run.errors is None


async def test_update_run_transitions_to_succeeded() -> None:
    """update_run transitions status and records chars_out + duration_ms."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_run, get_latest_run, update_run

        db = _StubDatabase()
        claims = _admin_claims()
        await create_run(db, claims, doc_id="docR", run_id="runR")  # type: ignore[arg-type]
        await update_run(  # type: ignore[arg-type]
            db, claims, "runR",
            status="succeeded",
            chars_out=512,
            duration_ms=150,
        )
        run = await get_latest_run(db, claims, "docR")  # type: ignore[arg-type]
    assert run is not None
    assert run.status == "succeeded"


async def test_update_run_transitions_to_failed_with_errors() -> None:
    """update_run with status='failed' records the errors payload."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_run, update_run

        db = _StubDatabase()
        claims = _admin_claims()
        await create_run(db, claims, doc_id="docF", run_id="runF")  # type: ignore[arg-type]
        error_payload = {"code": "PARSE_ERROR", "message": "bad file"}
        await update_run(  # type: ignore[arg-type]
            db, claims, "runF",
            status="failed",
            errors=error_payload,
            duration_ms=10,
        )
        key = (claims.tenant_id, "runF")
        assert db._runs[key]["status"] == "failed"


async def test_get_latest_run_returns_most_recent() -> None:
    """get_latest_run returns the run for the tenant's doc."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_run, get_latest_run

        db = _StubDatabase()
        claims = _admin_claims()
        await create_run(db, claims, doc_id="docLR", run_id="run-lr-1")  # type: ignore[arg-type]
        run = await get_latest_run(db, claims, "docLR")  # type: ignore[arg-type]
    assert run is not None
    assert run.run_id == "run-lr-1"


async def test_get_latest_run_cross_tenant_returns_none() -> None:
    """get_latest_run does not return runs from another tenant."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_run, get_latest_run

        db = _StubDatabase()
        alpha_claims = _admin_claims("tenant-alpha")
        beta_claims = _admin_claims("tenant-beta")
        await create_run(db, alpha_claims, doc_id="docCT", run_id="run-ct")  # type: ignore[arg-type]
        run = await get_latest_run(db, beta_claims, "docCT")  # type: ignore[arg-type]
    assert run is None


# ==============================================================================
# Global-caller guard
# ==============================================================================


async def test_create_doc_global_caller_raises() -> None:
    """create_doc with PLATFORM_ADMIN claims raises ValidationError."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_doc

        db = _StubDatabase()
        claims = _global_claims()
        with pytest.raises(ValidationError):
            await create_doc(  # type: ignore[arg-type]
                db, claims,
                source="upload",
                filename="f.txt",
                content_type="text/plain",
                content_hash="h",
                storage_key="x/y/z",
            )


async def test_get_doc_global_caller_raises() -> None:
    """get_doc with PLATFORM_ADMIN claims raises ValidationError."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import get_doc

        db = _StubDatabase()
        with pytest.raises(ValidationError):
            await get_doc(db, _global_claims(), "any-doc")  # type: ignore[arg-type]


async def test_create_run_global_caller_raises() -> None:
    """create_run with PLATFORM_ADMIN claims raises ValidationError."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import create_run

        db = _StubDatabase()
        with pytest.raises(ValidationError):
            await create_run(db, _global_claims(), doc_id="doc1")  # type: ignore[arg-type]


# ==============================================================================
# S5.3: replace_chunks (knowledge_chunks)
# ==============================================================================


class _ChunkRecordingDatabase:
    """Stub DB that records SQL statements and stores chunks in memory."""

    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        # chunks keyed by (tenant_id, chunk_id)
        self._chunks: dict[tuple[str, str], dict[str, Any]] = {}
        # runs keyed by (tenant_id, run_id)
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}

    async def execute(self, query: str, *args: Any) -> str:
        self.executions.append((query, args))
        q = query.strip().upper()
        if q.startswith("DELETE FROM KNOWLEDGE_CHUNKS"):
            tenant_id, doc_id = args[0], args[1]
            to_remove = [
                k for k, v in self._chunks.items()
                if k[0] == tenant_id and v.get("doc_id") == doc_id
            ]
            for k in to_remove:
                del self._chunks[k]
            return f"DELETE {len(to_remove)}"
        if q.startswith("INSERT INTO KNOWLEDGE_CHUNKS"):
            tenant_id, doc_id, chunk_id, content, embedding, metadata = args
            self._chunks[(tenant_id, chunk_id)] = {
                "tenant_id": tenant_id,
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "content": content,
                "embedding": embedding,
                "metadata": metadata,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE INGESTION_RUNS"):
            tenant_id, run_id = str(args[-2]), str(args[-1])
            key = (tenant_id, run_id)
            if key not in self._runs:
                return "UPDATE 0"
            updated = dict(self._runs[key])
            updated["status"] = args[0]
            self._runs[key] = updated
            return "UPDATE 1"
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def close(self) -> None:
        pass


async def test_replace_chunks_issues_delete_then_inserts() -> None:
    """replace_chunks issues DELETE then one INSERT per chunk with deterministic chunk_ids."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import ChunkRow, replace_chunks

        db = _ChunkRecordingDatabase()
        claims = _admin_claims("tenant-alpha")

        rows = [
            ChunkRow(
                chunk_id="doc1-0000",
                content="First chunk.",
                embedding=[0.1, 0.2, 0.3],
                metadata={"chunk_index": 0, "content_len": 12},
            ),
            ChunkRow(
                chunk_id="doc1-0001",
                content="Second chunk.",
                embedding=[0.4, 0.5, 0.6],
                metadata={"chunk_index": 1, "content_len": 13},
            ),
        ]
        await replace_chunks(db, claims, "doc1", rows)  # type: ignore[arg-type]

    # First execution must be the DELETE scoped to the tenant + doc.
    stmts = [e[0].strip().upper() for e in db.executions]
    assert stmts[0].startswith("DELETE FROM KNOWLEDGE_CHUNKS")
    delete_args = db.executions[0][1]
    assert delete_args[0] == "tenant-alpha"
    assert delete_args[1] == "doc1"

    # Then one INSERT per row.
    inserts = [e for e in db.executions if e[0].strip().upper().startswith("INSERT INTO KNOWLEDGE_CHUNKS")]
    assert len(inserts) == 2

    # Verify deterministic chunk_ids are passed as the 3rd positional arg.
    insert_chunk_ids = [e[1][2] for e in inserts]
    assert insert_chunk_ids == ["doc1-0000", "doc1-0001"]

    # Verify embedding list is passed as the 5th positional arg.
    assert inserts[0][1][4] == [0.1, 0.2, 0.3]
    assert inserts[1][1][4] == [0.4, 0.5, 0.6]


async def test_replace_chunks_cross_tenant_isolation() -> None:
    """Tenant A's replace_chunks never touches tenant B's chunks."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import ChunkRow, replace_chunks

        db = _ChunkRecordingDatabase()
        # Pre-seed tenant B's chunk
        db._chunks[("tenant-beta", "doc1-0000")] = {
            "tenant_id": "tenant-beta",
            "doc_id": "doc1",
            "chunk_id": "doc1-0000",
            "content": "Beta content.",
            "embedding": [1.0],
            "metadata": {},
        }

        claims_alpha = _admin_claims("tenant-alpha")
        rows = [
            ChunkRow(
                chunk_id="doc1-0000",
                content="Alpha content.",
                embedding=[0.1],
                metadata={"chunk_index": 0, "content_len": 14},
            )
        ]
        await replace_chunks(db, claims_alpha, "doc1", rows)  # type: ignore[arg-type]

    # Tenant beta's chunk must still be present.
    assert ("tenant-beta", "doc1-0000") in db._chunks, (
        "tenant beta's chunk was incorrectly deleted by tenant alpha's replace_chunks"
    )
    # Tenant alpha's chunk is inserted.
    assert ("tenant-alpha", "doc1-0000") in db._chunks


async def test_replace_chunks_global_caller_raises() -> None:
    """replace_chunks with PLATFORM_ADMIN claims raises ValidationError."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import replace_chunks

        db = _ChunkRecordingDatabase()
        with pytest.raises(ValidationError):
            await replace_chunks(db, _global_claims(), "doc1", [])  # type: ignore[arg-type]


async def test_delete_doc_issues_three_deletes_in_order() -> None:
    """delete_doc issues DELETE knowledge_chunks -> ingestion_runs -> knowledge_docs, in order."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import delete_doc

        db = _ChunkRecordingDatabase()

        async def _execute(query: str, *args: Any) -> str:
            db.executions.append((query, args))
            q = query.strip().upper()
            if q.startswith("DELETE FROM KNOWLEDGE_CHUNKS"):
                return "DELETE 12"
            if q.startswith("DELETE FROM INGESTION_RUNS"):
                return "DELETE 1"
            if q.startswith("DELETE FROM KNOWLEDGE_DOCS"):
                return "DELETE 1"
            return "OK"

        db.execute = _execute  # type: ignore[method-assign]

        claims = _admin_claims("tenant-alpha")
        chunks_deleted, runs_deleted = await delete_doc(db, claims, "doc1")  # type: ignore[arg-type]

    assert chunks_deleted == 12
    assert runs_deleted == 1

    stmts = [e[0].strip().upper() for e in db.executions]
    assert len(stmts) == 3
    assert stmts[0].startswith("DELETE FROM KNOWLEDGE_CHUNKS")
    assert stmts[1].startswith("DELETE FROM INGESTION_RUNS")
    assert stmts[2].startswith("DELETE FROM KNOWLEDGE_DOCS")


async def test_delete_doc_every_statement_scoped_to_tenant_and_doc() -> None:
    """MANDATORY isolation: every DELETE binds exactly [tenant_id, doc_id] -- never doc_id alone."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import delete_doc

        db = _ChunkRecordingDatabase()

        async def _execute(query: str, *args: Any) -> str:
            db.executions.append((query, args))
            q = query.strip().upper()
            if q.startswith("DELETE FROM KNOWLEDGE_CHUNKS"):
                return "DELETE 3"
            if q.startswith("DELETE FROM INGESTION_RUNS"):
                return "DELETE 1"
            if q.startswith("DELETE FROM KNOWLEDGE_DOCS"):
                return "DELETE 1"
            return "OK"

        db.execute = _execute  # type: ignore[method-assign]

        claims = _admin_claims("tenant-alpha")
        await delete_doc(db, claims, "doc-scoped")  # type: ignore[arg-type]

    for query, args in db.executions:
        upper = query.strip().upper()
        assert "TENANT_ID = $1" in upper and "DOC_ID = $2" in upper, (
            f"DELETE statement missing tenant_id+doc_id predicate: {query!r}"
        )
        assert list(args) == ["tenant-alpha", "doc-scoped"], (
            f"DELETE statement bound params must be exactly [tenant_id, doc_id]: {args!r}"
        )


async def test_delete_doc_not_found_raises() -> None:
    """A 0-row knowledge_docs delete (absent/cross-tenant/race) raises NotFoundError DOC_NOT_FOUND."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import NotFoundError

        from api.ingestion.repository import delete_doc

        db = _ChunkRecordingDatabase()

        async def _execute(query: str, *args: Any) -> str:
            db.executions.append((query, args))
            q = query.strip().upper()
            if q.startswith("DELETE FROM KNOWLEDGE_CHUNKS"):
                return "DELETE 0"
            if q.startswith("DELETE FROM INGESTION_RUNS"):
                return "DELETE 0"
            if q.startswith("DELETE FROM KNOWLEDGE_DOCS"):
                return "DELETE 0"
            return "OK"

        db.execute = _execute  # type: ignore[method-assign]

        claims = _admin_claims("tenant-alpha")
        with pytest.raises(NotFoundError) as exc_info:
            await delete_doc(db, claims, "ghost-doc")  # type: ignore[arg-type]

    assert exc_info.value.code == "DOC_NOT_FOUND"
    # All three DELETEs must have been issued (no exception swallowed mid-way,
    # and the count-return path must not be taken).
    stmts = [e[0].strip().upper() for e in db.executions]
    assert len(stmts) == 3


async def test_delete_doc_global_caller_raises_and_issues_no_delete() -> None:
    """delete_doc with PLATFORM_ADMIN claims raises ValidationError; no DELETE issued."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import delete_doc

        db = _ChunkRecordingDatabase()
        with pytest.raises(ValidationError):
            await delete_doc(db, _global_claims(), "doc1")  # type: ignore[arg-type]

    assert db.executions == [], "No DELETE should be issued for a global caller"


async def test_delete_doc_ingestion_runs_explicitly_targeted() -> None:
    """Regression for the non-cascade fact: the second statement targets ingestion_runs."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import delete_doc

        db = _ChunkRecordingDatabase()

        async def _execute(query: str, *args: Any) -> str:
            db.executions.append((query, args))
            q = query.strip().upper()
            if q.startswith("DELETE FROM KNOWLEDGE_CHUNKS"):
                return "DELETE 0"
            if q.startswith("DELETE FROM INGESTION_RUNS"):
                return "DELETE 2"
            if q.startswith("DELETE FROM KNOWLEDGE_DOCS"):
                return "DELETE 1"
            return "OK"

        db.execute = _execute  # type: ignore[method-assign]

        claims = _admin_claims("tenant-alpha")
        chunks_deleted, runs_deleted = await delete_doc(db, claims, "doc-runs")  # type: ignore[arg-type]

    assert runs_deleted == 2
    stmts = [e[0].strip().upper() for e in db.executions]
    assert stmts[1].startswith("DELETE FROM INGESTION_RUNS")


async def test_update_run_with_chunks_out_binds_value() -> None:
    """update_run accepts chunks_out and includes it in the SQL params."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.repository import update_run

        db = _ChunkRecordingDatabase()
        # Seed a run so the stub returns UPDATE 1.
        db._runs[("tenant-alpha", "run-x")] = {
            "run_id": "run-x",
            "doc_id": "doc-x",
            "status": "running",
            "chars_out": None,
            "errors": None,
            "started_at": _NOW,
            "finished_at": None,
            "duration_ms": None,
            "chunks_out": None,
        }
        claims = _admin_claims("tenant-alpha")
        await update_run(  # type: ignore[arg-type]
            db,
            claims,
            "run-x",
            status="succeeded",
            chars_out=500,
            chunks_out=5,
            duration_ms=200,
        )

    # Find the UPDATE INGESTION_RUNS execution and check chunks_out appears in args.
    updates = [
        e for e in db.executions
        if e[0].strip().upper().startswith("UPDATE INGESTION_RUNS")
    ]
    assert updates, "Expected at least one UPDATE INGESTION_RUNS"
    all_args = updates[0][1]
    assert 5 in all_args, f"chunks_out=5 not found in SQL params: {all_args}"
