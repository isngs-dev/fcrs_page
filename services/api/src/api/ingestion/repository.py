"""Ingestion repository — tenant-scoped async SQL for knowledge_docs + ingestion_runs.

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …),
  building them with ``f"${len(params)}"`` — never a hardcoded index.
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

Data model (migration 0010):
- ``knowledge_docs(tenant_id PK, doc_id PK, source, filename, content_type,
  status, content_hash, storage_key, created_at, updated_at)``
  UNIQUE (tenant_id, content_hash).
- ``ingestion_runs(tenant_id PK, run_id PK, doc_id, status, chars_out, errors
  jsonb, started_at, finished_at, duration_ms)``.

Data model (migration 0011):
- ``knowledge_chunks(tenant_id PK, doc_id, chunk_id PK, content, embedding
  vector(768), metadata jsonb, created_at)`` FK → knowledge_docs ON DELETE CASCADE.
- ``ingestion_runs.chunks_out integer`` — number of chunks written.
- ``tenant_llm_configs.embedding_model text`` — per-tenant embedding model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import NotFoundError, ValidationError  # noqa: F401

# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkRow:
    """A single knowledge chunk ready for insertion into ``knowledge_chunks``.

    ``embedding`` is a plain Python ``list[float]`` — the pgvector codec on the
    asyncpg connection (registered via ``register_vector_init``) handles the
    wire encoding. ``metadata`` is a plain dict (→ jsonb via the default codec).
    """

    chunk_id: str
    content: str
    embedding: list[float]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class KnowledgeDoc:
    doc_id: str
    source: str
    filename: str
    content_type: str
    status: str
    content_hash: str
    storage_key: str
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    description: str | None = None
    uploaded_by: str | None = None


@dataclass(frozen=True)
class IngestionRun:
    run_id: str
    doc_id: str
    status: str
    chars_out: int | None
    errors: list[Any] | dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Ingestion is always tenant-scoped; a global caller has no tenant_id and
    therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Ingestion repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


# ---------------------------------------------------------------------------
# knowledge_docs
# ---------------------------------------------------------------------------


async def create_doc(
    db: Database,
    claims: AuthClaims,
    *,
    source: str,
    filename: str,
    content_type: str,
    content_hash: str,
    storage_key: str,
    doc_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    uploaded_by: str | None = None,
) -> KnowledgeDoc:
    """Insert a new ``knowledge_docs`` row with ``status='pending'``.

    Returns the resulting ``KnowledgeDoc``. ``doc_id`` defaults to a fresh
    ``uuid4().hex``; the caller may supply one for idempotency. ``title``/
    ``description`` are admin-supplied, optional metadata (Knowledge Base
    list feature); ``uploaded_by`` is the uploading admin's user id
    (``AuthClaims.subject``) for provenance/display, never accepted from a
    request body.
    """
    _reject_global(claims)

    new_id = doc_id or uuid4().hex
    params: list[Any] = [
        claims.tenant_id,
        new_id,
        source,
        filename,
        content_type,
        "pending",
        content_hash,
        storage_key,
        title,
        description,
        uploaded_by,
    ]
    await db.execute(
        "INSERT INTO knowledge_docs "
        "(tenant_id, doc_id, source, filename, content_type, status, "
        " content_hash, storage_key, title, description, uploaded_by) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        *params,
    )
    row = await db.fetchrow(
        "SELECT doc_id, source, filename, content_type, status, content_hash, "
        "storage_key, created_at, updated_at, title, description, uploaded_by "
        "FROM knowledge_docs "
        "WHERE tenant_id = $1 AND doc_id = $2",
        claims.tenant_id,
        new_id,
    )
    # Should never be None — we just inserted it.
    assert row is not None  # noqa: S101
    return _row_to_doc(row)


async def find_doc_by_hash(
    db: Database,
    claims: AuthClaims,
    content_hash: str,
) -> KnowledgeDoc | None:
    """Return the existing doc for ``(tenant_id, content_hash)``, or ``None``.

    Used to implement idempotent re-upload (decision 5 in S5.2): if a document
    with the same SHA-256 hash already exists for this tenant, return it without
    re-inserting or re-enqueuing.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT doc_id, source, filename, content_type, status, content_hash, "
        "storage_key, created_at, updated_at, title, description, uploaded_by "
        "FROM knowledge_docs "
        "WHERE tenant_id = $1 AND content_hash = $2",
        claims.tenant_id,
        content_hash,
    )
    return _row_to_doc(row) if row is not None else None


async def get_doc(
    db: Database,
    claims: AuthClaims,
    doc_id: str,
) -> KnowledgeDoc | None:
    """Fetch a doc by ``doc_id`` scoped to the caller's tenant, or ``None``."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT doc_id, source, filename, content_type, status, content_hash, "
        "storage_key, created_at, updated_at, title, description, uploaded_by "
        "FROM knowledge_docs "
        "WHERE tenant_id = $1 AND doc_id = $2",
        claims.tenant_id,
        doc_id,
    )
    return _row_to_doc(row) if row is not None else None


async def list_docs(
    db: Database,
    claims: AuthClaims,
) -> list[KnowledgeDoc]:
    """Return all of the caller's tenant's docs, newest upload first.

    Knowledge Base list feature: backs ``GET /admin/ingestion/docs``. The
    ``LIMIT`` is a cheap defensive cap against a runaway result set, not real
    pagination — no offset/page params, since realistic per-tenant document
    counts are small and nothing has asked for pagination. Uses the existing
    ``idx_knowledge_docs_tenant_created (tenant_id, created_at)`` index
    (migration 0010) — no new index needed.
    """
    _reject_global(claims)

    rows = await db.fetch(
        "SELECT doc_id, source, filename, content_type, status, content_hash, "
        "storage_key, created_at, updated_at, title, description, uploaded_by "
        "FROM knowledge_docs "
        "WHERE tenant_id = $1 "
        "ORDER BY created_at DESC "
        "LIMIT 1000",
        claims.tenant_id,
    )
    return [_row_to_doc(row) for row in rows]


async def update_doc_status(
    db: Database,
    claims: AuthClaims,
    doc_id: str,
    status: str,
) -> None:
    """Update the ``status`` column for a doc, also setting ``updated_at = now()``.

    Raises ``NotFoundError`` (``DOC_NOT_FOUND``) if the doc does not exist or
    is not visible to the caller's tenant.
    """
    _reject_global(claims)

    params: list[Any] = [status, claims.tenant_id, doc_id]
    result = await db.execute(
        "UPDATE knowledge_docs "
        "SET status = $1, updated_at = now() "
        "WHERE tenant_id = $2 AND doc_id = $3",
        *params,
    )
    # asyncpg returns "UPDATE <n>"; 0 means the row was not found.
    parts = result.split()
    if len(parts) == 2 and parts[0].upper() == "UPDATE" and parts[1] == "0":
        raise NotFoundError(
            "Knowledge document not found.",
            code="DOC_NOT_FOUND",
        )


# ---------------------------------------------------------------------------
# ingestion_runs
# ---------------------------------------------------------------------------


async def create_run(
    db: Database,
    claims: AuthClaims,
    *,
    doc_id: str,
    run_id: str | None = None,
) -> IngestionRun:
    """Insert a new ``ingestion_runs`` row with ``status='queued'``.

    Returns the resulting ``IngestionRun``. ``run_id`` defaults to a fresh
    ``uuid4().hex``; the caller may supply one for idempotency.
    """
    _reject_global(claims)

    new_run_id = run_id or uuid4().hex
    params: list[Any] = [claims.tenant_id, new_run_id, doc_id, "queued"]
    await db.execute(
        "INSERT INTO ingestion_runs (tenant_id, run_id, doc_id, status) "
        "VALUES ($1, $2, $3, $4)",
        *params,
    )
    row = await db.fetchrow(
        "SELECT run_id, doc_id, status, chars_out, errors, started_at, "
        "finished_at, duration_ms "
        "FROM ingestion_runs "
        "WHERE tenant_id = $1 AND run_id = $2",
        claims.tenant_id,
        new_run_id,
    )
    assert row is not None  # noqa: S101
    return _row_to_run(row)


async def update_run(
    db: Database,
    claims: AuthClaims,
    run_id: str,
    *,
    status: str,
    chars_out: int | None = None,
    errors: list[Any] | dict[str, Any] | None = None,
    duration_ms: int | None = None,
    finished_at: datetime | None = None,
    chunks_out: int | None = None,
) -> None:
    """Update a run's fields (status, optional result columns).

    Builds the SET clause positionally; only non-``None`` optional columns are
    included to avoid overwriting prior values accidentally.

    Raises ``NotFoundError`` (``RUN_NOT_FOUND``) if the run does not exist.
    """
    _reject_global(claims)

    params: list[Any] = [status]
    set_clauses = ["status = $1"]

    if chars_out is not None:
        params.append(chars_out)
        set_clauses.append(f"chars_out = ${len(params)}")

    if errors is not None:
        params.append(errors)
        set_clauses.append(f"errors = ${len(params)}")

    if duration_ms is not None:
        params.append(duration_ms)
        set_clauses.append(f"duration_ms = ${len(params)}")

    if finished_at is not None:
        params.append(finished_at)
        set_clauses.append(f"finished_at = ${len(params)}")
    else:
        # Always set finished_at to now() when completing (succeeded/failed).
        if status in ("succeeded", "failed"):
            set_clauses.append("finished_at = now()")

    if chunks_out is not None:
        params.append(chunks_out)
        set_clauses.append(f"chunks_out = ${len(params)}")

    params.append(claims.tenant_id)
    params.append(run_id)
    where_idx_tenant = len(params) - 1
    where_idx_run = len(params)

    # set_clauses contains only safe column-name strings from the function body;
    # all values are in params (positional placeholders). The dynamic clause
    # construction here is safe — no user input flows into set_clauses.
    # ruff: noqa: S608
    set_clause_str = ", ".join(set_clauses)
    sql = (
        f"UPDATE ingestion_runs SET {set_clause_str} "  # noqa: S608
        f"WHERE tenant_id = ${where_idx_tenant} AND run_id = ${where_idx_run}"
    )
    result = await db.execute(sql, *params)
    parts = result.split()
    if len(parts) == 2 and parts[0].upper() == "UPDATE" and parts[1] == "0":
        raise NotFoundError(
            "Ingestion run not found.",
            code="RUN_NOT_FOUND",
        )


async def replace_chunks(
    db: Database,
    claims: AuthClaims,
    doc_id: str,
    rows: list[ChunkRow],
) -> None:
    """Replace all chunks for ``(tenant_id, doc_id)`` with ``rows`` — idempotent.

    Implements S5.3 decision 5: DELETE existing chunks for the doc, then INSERT
    the new set. This is safe for Celery retries because re-running always
    produces the same final set of rows (same deterministic chunk_ids + content).
    A re-parse that produces a *different chunk count* leaves no stale chunks.

    ``_reject_global`` is called first; VISITOR never reaches ingestion.

    Transaction note: ``common.db.Database`` does not expose a transaction
    helper. We issue DELETE then one INSERT per chunk sequentially. asyncpg
    auto-wraps each ``pool.execute`` in a single-statement transaction, so
    partial failure on an INSERT is retryable via Celery (the next retry starts
    fresh with DELETE again). This is documented explicitly per the spec.
    """
    _reject_global(claims)

    # Step 1 — delete all existing chunks for this (tenant, doc) pair.
    params: list[Any] = [claims.tenant_id, doc_id]
    await db.execute(
        "DELETE FROM knowledge_chunks WHERE tenant_id = $1 AND doc_id = $2",
        *params,
    )

    # Step 2 — insert each chunk.
    for row in rows:
        insert_params: list[Any] = [
            claims.tenant_id,
            doc_id,
            row.chunk_id,
            row.content,
            row.embedding,  # list[float] → vector via pgvector codec
            row.metadata,   # dict → jsonb via default codec
        ]
        await db.execute(
            "INSERT INTO knowledge_chunks "
            "(tenant_id, doc_id, chunk_id, content, embedding, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            *insert_params,
        )


async def delete_doc(
    db: Database,
    claims: AuthClaims,
    doc_id: str,
) -> tuple[int, int]:
    """Hard-delete a doc's DB footprint, tenant-scoped.

    Returns ``(chunks_deleted, runs_deleted)``.

    Order (SR-4 decision — see Investigation in the sprint spec):
      1. ``DELETE FROM knowledge_chunks WHERE tenant_id=$1 AND doc_id=$2``
         -> chunks_deleted. Redundant with the migration-0011 FK
         ``ON DELETE CASCADE``, but issued explicitly so the count is known
         and the delete is self-contained/independent of FK config.
      2. ``DELETE FROM ingestion_runs WHERE tenant_id=$1 AND doc_id=$2``
         -> runs_deleted. NOT cascaded — ``ingestion_runs`` FKs to
         ``tenants``, not ``knowledge_docs`` (migration 0010) — so this must
         be explicit or the run rows orphan.
      3. ``DELETE FROM knowledge_docs WHERE tenant_id=$1 AND doc_id=$2`` — the
         authoritative row; a cascade would also drop chunks, already gone
         from step 1.

    Raises ``NotFoundError`` (``DOC_NOT_FOUND``) if step 3 removes 0 rows
    (absent / cross-tenant / concurrent-delete race). Rejects global
    (PLATFORM_ADMIN) callers via ``_reject_global``.

    Every statement is ``WHERE tenant_id = $1 AND doc_id = $2`` — parameterized,
    positional, asyncpg, no ORM, never ``doc_id`` alone.
    """
    _reject_global(claims)

    params: list[Any] = [claims.tenant_id, doc_id]

    chunks_result = await db.execute(
        "DELETE FROM knowledge_chunks WHERE tenant_id = $1 AND doc_id = $2",
        *params,
    )
    chunks_deleted = _parse_delete_count(chunks_result)

    runs_result = await db.execute(
        "DELETE FROM ingestion_runs WHERE tenant_id = $1 AND doc_id = $2",
        *params,
    )
    runs_deleted = _parse_delete_count(runs_result)

    docs_result = await db.execute(
        "DELETE FROM knowledge_docs WHERE tenant_id = $1 AND doc_id = $2",
        *params,
    )
    if _parse_delete_count(docs_result) == 0:
        raise NotFoundError(
            "Knowledge document not found.",
            code="DOC_NOT_FOUND",
        )

    return chunks_deleted, runs_deleted


async def get_latest_run(
    db: Database,
    claims: AuthClaims,
    doc_id: str,
) -> IngestionRun | None:
    """Return the most-recently-started run for ``doc_id``, or ``None``."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT run_id, doc_id, status, chars_out, errors, started_at, "
        "finished_at, duration_ms "
        "FROM ingestion_runs "
        "WHERE tenant_id = $1 AND doc_id = $2 "
        "ORDER BY started_at DESC, run_id DESC "
        "LIMIT 1",
        claims.tenant_id,
        doc_id,
    )
    return _row_to_run(row) if row is not None else None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_delete_count(result: str) -> int:
    """Parse asyncpg's ``"DELETE <n>"`` command tag into ``<n>``.

    Mirrors the ``"UPDATE <n>"`` parsing already used by ``update_doc_status``/
    ``update_run``. Returns 0 if the tag is unrecognized (defensive; asyncpg
    always returns this shape for a DELETE).
    """
    parts = result.split()
    if len(parts) == 2 and parts[0].upper() == "DELETE":
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0


def _row_to_doc(row: Any) -> KnowledgeDoc:
    return KnowledgeDoc(
        doc_id=str(row["doc_id"]),
        source=str(row["source"]),
        filename=str(row["filename"]),
        content_type=str(row["content_type"]),
        status=str(row["status"]),
        content_hash=str(row["content_hash"]),
        storage_key=str(row["storage_key"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        # .get(), not row[...]: the live DB always has these columns
        # post-migration-0054, but pre-existing test stub seed dicts that
        # don't yet carry these keys should degrade to None, not KeyError.
        title=row.get("title"),
        description=row.get("description"),
        uploaded_by=row.get("uploaded_by"),
    )


def _row_to_run(row: Any) -> IngestionRun:
    return IngestionRun(
        run_id=str(row["run_id"]),
        doc_id=str(row["doc_id"]),
        status=str(row["status"]),
        chars_out=row["chars_out"],
        errors=row["errors"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
    )
