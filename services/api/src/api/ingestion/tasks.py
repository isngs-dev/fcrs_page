"""Celery task: ingestion.ingest_document.

This task implements S5.2 decision 6 (preserved) and S5.3 decisions 3, 5, 6:
  1. Mark run ``running``.
  2. Load raw bytes from storage.
  3. Parse to text.
  4. Store ``parsed.txt`` in storage.
  5. Resolve embedding config; chunk + embed + store chunks (S5.3).
  6. On success  → run ``succeeded`` (chars_out, chunks_out, duration_ms) + doc ``parsed``.
  7. On parse/validation error (deterministic) → run ``failed`` (errors recorded)
     + doc ``failed``. Do NOT raise; do NOT retry — the file will never parse.
  8. On ``EMBEDDING_NOT_CONFIGURED`` / ``EMBEDDING_DIM_MISMATCH`` (deterministic) →
     run ``failed`` + doc ``failed``. Do NOT raise; do NOT retry.
  9. On transient infra error (storage/DB) → let it propagate so Celery
     retries via worker-crash redelivery (``task_acks_late`` /
     ``task_reject_on_worker_lost``, S5.1). Never swallow.

S12.6 (embed failure handling -- see dev_plan/HANDOFF_embedding_batch_timeout_fix.md §7):
  ``LLMError`` from the embed step (Step 8) is auto-retried by Celery
  (``autoretry_for=(LLMError,)`` on the task decorator, bounded, with
  backoff+jitter). Once retries are exhausted, the final ``LLMError`` is
  caught in ``_execute`` and marks the run/doc ``failed`` (mirroring the
  ``EMBEDDING_DIM_MISMATCH`` handling immediately below it) instead of
  leaving ``ingestion_runs.status`` stuck at ``running`` forever. Step 10's
  chunk replace (DELETE + INSERT, deterministic chunk_ids) is idempotent, so
  a retried run re-running the full parse/chunk/embed pipeline from the top
  is safe.

Worker tenant context (decision 1):
  The upload route passes ``tenant_id`` — which came from the admin JWT — as an
  explicit task kwarg. The task builds a tenant-scoped ``AuthClaims`` with
  ``subject="system:ingestion"`` and ``role=Role.CLIENT_ADMIN`` so that every
  repository call is still filtered at the repository layer.

correlation_id (decision 2):
  MUST be declared in the signature. Celery runs ``check_arguments`` inside
  ``apply_async`` at enqueue time, before the base ``_CorrelationTask.__call__``
  can consume it. Omitting it makes ``.delay(correlation_id=...)`` raise
  ``TypeError`` at enqueue — the same bug caught in S5.1.

pgvector codec (S5.3 decision 3):
  ``Database.connect(...)`` now passes ``init=register_vector_init`` so that both
  the default jsonb codec AND the pgvector vector codec are registered on every
  connection. Without this, writing a ``vector(768)`` column raises at runtime.
"""
from __future__ import annotations

import asyncio
import time

from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import ValidationError
from common.logging import get_logger
from common.pgvector import register_vector_init

from api.ingestion import repository as repo
from api.ingestion.chunker import chunk_text
from api.ingestion.parsers import parse
from api.ingestion.storage import StorageProvider, get_storage
from api.llm.config_repository import get_llm_config
from api.llm.factory import provider_for
from api.llm.provider import LLMError
from api.notifications.emit import emit_event_safe
from api.tasks.celery_app import _CorrelationTask, celery_app

_log = get_logger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="ingestion.ingest_document",
    base=_CorrelationTask,
    # S12.6: embedding failures are retry-worthy (distinct from generation,
    # which is "transient only" -- see handoff doc §6b's handbook §18
    # cross-check). max_retries=3 matches the existing _CorrelationTask base
    # convention (api/tasks/celery_app.py) -- no other task in this codebase
    # overrides it, so this keeps the same bound explicit on this task too.
    autoretry_for=(LLMError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def ingest_document(
    self: _CorrelationTask,
    *,
    doc_id: str,
    tenant_id: str,
    run_id: str,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, object]:
    """Parse a document and record the result in the run log.

    Parameters
    ----------
    doc_id:
        The ``knowledge_docs.doc_id`` to parse.
    tenant_id:
        Trusted tenant identifier. Originates from the admin JWT at enqueue
        time — never from visitor input (S5.2 decision 1).
    run_id:
        The ``ingestion_runs.run_id`` to update throughout execution.
    correlation_id:
        Must be declared here (see module docstring / S5.2 decision 2).
        The value is consumed by ``_CorrelationTask.__call__`` before this
        body runs; it will always be ``None`` here.

    Returns
    -------
    dict
        ``{"doc_id": ..., "run_id": ..., "status": "succeeded"|"failed"}``.
    """
    # Build a tenant-scoped service identity — repo calls are still filtered
    # by tenant_id even though we're outside an HTTP request (decision 1).
    claims = AuthClaims(
        subject="system:ingestion",
        role=Role.CLIENT_ADMIN,
        tenant_id=tenant_id,
    )

    storage = get_storage()

    # We need a running asyncio event loop to call the async repository. In
    # the Celery worker (threads, not coroutines) we create a temporary loop.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _run(self, claims, doc_id, run_id, storage),
        )
    finally:
        loop.close()


async def _run(
    task: _CorrelationTask,
    claims: AuthClaims,
    doc_id: str,
    run_id: str,
    storage: StorageProvider,
) -> dict[str, object]:
    """Async inner body of ``ingest_document``."""
    from api.config import get_api_settings  # noqa: PLC0415

    settings = get_api_settings()
    # S5.3 decision 3: register the pgvector codec alongside the default jsonb
    # codec so that list[float] → vector column writes succeed at runtime.
    db = await Database.connect(
        settings.database_url,
        statement_cache_size=0,
        init=register_vector_init,
    )
    try:
        return await _execute(task, db, claims, doc_id, run_id, storage)
    finally:
        await db.close()


async def _execute(
    task: _CorrelationTask,
    db: Database,
    claims: AuthClaims,
    doc_id: str,
    run_id: str,
    storage: StorageProvider,
) -> dict[str, object]:
    """Core parse-chunk-embed-store logic, given an open DB connection."""
    from api.config import get_api_settings  # noqa: PLC0415

    settings = get_api_settings()

    _log.info(
        "ingest_document started",
        extra={
            "task": "ingestion.ingest_document",
            "event": "task_started",
            "doc_id": doc_id,
            "run_id": run_id,
        },
    )

    # Step 1 — mark run running.
    await repo.update_run(db, claims, run_id, status="running")

    # Step 2 — fetch doc to learn storage_key + content_type.
    doc = await repo.get_doc(db, claims, doc_id)
    if doc is None:
        # This would be a programming error (upload route always creates the doc
        # before enqueuing). Treat as transient so Celery can retry.
        raise RuntimeError(f"Doc {doc_id!r} not found for tenant {claims.tenant_id!r}.")

    t0 = time.monotonic()

    try:
        # Step 3 — load bytes from storage.
        raw_bytes: bytes = storage.get(doc.storage_key)

        # Step 4 — parse.
        parsed_text = parse(doc.content_type, raw_bytes)

        # Step 5 — store parsed.txt next to the raw file.
        parsed_key = f"{claims.tenant_id}/{doc_id}/parsed.txt"
        storage.put(parsed_key, parsed_text.encode("utf-8"))

    except ValidationError as exc:
        # Deterministic parse/validation error — record + fail, do NOT retry.
        duration_ms = int((time.monotonic() - t0) * 1000)
        error_entry = {"code": exc.code, "message": exc.message}
        await repo.update_run(
            db,
            claims,
            run_id,
            status="failed",
            errors=error_entry,
            duration_ms=duration_ms,
        )
        await repo.update_doc_status(db, claims, doc_id, "failed")

        _log.warning(
            "ingest_document parse error",
            extra={
                "task": "ingestion.ingest_document",
                "event": "task_parse_error",
                "doc_id": doc_id,
                "run_id": run_id,
                "error_code": exc.code,
            },
        )
        # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable
        # run/doc "failed" writes above. payload is ids only.
        await emit_event_safe(
            db,
            claims,
            kind="ingestion_failed",
            category="system",
            target_type="ingestion_run",
            target_id=run_id,
            payload={"doc_id": doc_id, "run_id": run_id, "error_code": exc.code},
            actor_id=None,
        )
        return {"doc_id": doc_id, "run_id": run_id, "status": "failed"}

    # -----------------------------------------------------------------------
    # S5.3: chunk → embed → store
    # -----------------------------------------------------------------------

    # Step 6 — resolve tenant embedding config.
    config = await get_llm_config(db, claims)
    if config is None or not config.embedding_model:
        # Deterministic failure: no embedding model configured.
        duration_ms = int((time.monotonic() - t0) * 1000)
        error_entry = {
            "code": "EMBEDDING_NOT_CONFIGURED",
            "message": "No embedding model is configured for this tenant.",
        }
        await repo.update_run(
            db,
            claims,
            run_id,
            status="failed",
            errors=error_entry,
            duration_ms=duration_ms,
        )
        await repo.update_doc_status(db, claims, doc_id, "failed")
        _log.warning(
            "ingest_document embedding not configured",
            extra={
                "task": "ingestion.ingest_document",
                "event": "task_embedding_not_configured",
                "doc_id": doc_id,
                "run_id": run_id,
            },
        )
        # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable
        # run/doc "failed" writes above. payload is ids only.
        await emit_event_safe(
            db,
            claims,
            kind="ingestion_failed",
            category="system",
            target_type="ingestion_run",
            target_id=run_id,
            payload={"doc_id": doc_id, "run_id": run_id, "error_code": "EMBEDDING_NOT_CONFIGURED"},
            actor_id=None,
        )
        return {"doc_id": doc_id, "run_id": run_id, "status": "failed"}

    # Step 7 — chunk the parsed text.
    chunks = chunk_text(
        parsed_text,
        max_chars=settings.chunk_max_chars,
        overlap=settings.chunk_overlap_chars,
    )

    if not chunks:
        # Empty text after parsing: succeed with chunks_out=0.
        duration_ms = int((time.monotonic() - t0) * 1000)
        chars_out = len(parsed_text)
        await repo.update_run(
            db,
            claims,
            run_id,
            status="succeeded",
            chars_out=chars_out,
            chunks_out=0,
            duration_ms=duration_ms,
        )
        await repo.update_doc_status(db, claims, doc_id, "parsed")
        _log.info(
            "ingest_document succeeded (no chunks)",
            extra={
                "task": "ingestion.ingest_document",
                "event": "task_completed",
                "doc_id": doc_id,
                "run_id": run_id,
                "chars_out": chars_out,
                "chunks_out": 0,
                "duration_ms": duration_ms,
            },
        )
        return {"doc_id": doc_id, "run_id": run_id, "status": "succeeded"}

    # Step 8 — embed chunks. LLMError is auto-retried by Celery (bounded,
    # backoff+jitter via autoretry_for on the task decorator); once retries
    # are exhausted, the final LLMError is caught below and marks the run
    # failed instead of leaving it stuck at "running" (S12.6 fix).
    llm_provider = provider_for(config)
    try:
        try:
            vectors = await llm_provider.embed(chunks, model=config.embedding_model)
        except LLMError as exc:
            if task.request.retries < task.max_retries:
                # Retries remain -- propagate so Celery's autoretry_for schedules
                # the next attempt with backoff+jitter.
                raise
            duration_ms = int((time.monotonic() - t0) * 1000)
            error_entry = {
                "code": "EMBEDDING_FAILED",
                "message": str(exc),
            }
            await repo.update_run(
                db,
                claims,
                run_id,
                status="failed",
                errors=error_entry,
                duration_ms=duration_ms,
            )
            await repo.update_doc_status(db, claims, doc_id, "failed")
            _log.warning(
                "ingest_document embedding failed (retries exhausted)",
                extra={
                    "task": "ingestion.ingest_document",
                    "event": "task_embedding_failed",
                    "doc_id": doc_id,
                    "run_id": run_id,
                    "retries": task.request.retries,
                },
            )
            # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable
            # run/doc "failed" writes above. payload is ids only.
            await emit_event_safe(
                db,
                claims,
                kind="ingestion_failed",
                category="system",
                target_type="ingestion_run",
                target_id=run_id,
                payload={"doc_id": doc_id, "run_id": run_id, "error_code": "EMBEDDING_FAILED"},
                actor_id=None,
            )
            return {"doc_id": doc_id, "run_id": run_id, "status": "failed"}
    finally:
        await llm_provider.aclose()

    # Step 9 — validate every vector dimension.
    for i, vec in enumerate(vectors):
        if len(vec) != settings.embedding_dimension:
            duration_ms = int((time.monotonic() - t0) * 1000)
            error_entry = {
                "code": "EMBEDDING_DIM_MISMATCH",
                "message": (
                    f"Vector {i} has dimension {len(vec)}, "
                    f"expected {settings.embedding_dimension}."
                ),
            }
            await repo.update_run(
                db,
                claims,
                run_id,
                status="failed",
                errors=error_entry,
                duration_ms=duration_ms,
            )
            await repo.update_doc_status(db, claims, doc_id, "failed")
            _log.warning(
                "ingest_document embedding dimension mismatch",
                extra={
                    "task": "ingestion.ingest_document",
                    "event": "task_embedding_dim_mismatch",
                    "doc_id": doc_id,
                    "run_id": run_id,
                    "vector_index": i,
                    "got": len(vec),
                    "expected": settings.embedding_dimension,
                },
            )
            # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable
            # run/doc "failed" writes above. payload is ids only.
            await emit_event_safe(
                db,
                claims,
                kind="ingestion_failed",
                category="system",
                target_type="ingestion_run",
                target_id=run_id,
                payload={
                    "doc_id": doc_id,
                    "run_id": run_id,
                    "error_code": "EMBEDDING_DIM_MISMATCH",
                },
                actor_id=None,
            )
            return {"doc_id": doc_id, "run_id": run_id, "status": "failed"}

    # Step 10 — idempotent replace chunks in the DB.
    chunk_rows = [
        repo.ChunkRow(
            chunk_id=f"{doc_id}-{i:04d}",
            content=chunk,
            embedding=list(vectors[i]),
            metadata={"chunk_index": i, "content_len": len(chunk)},
        )
        for i, chunk in enumerate(chunks)
    ]
    await repo.replace_chunks(db, claims, doc_id, chunk_rows)

    # Step 11 — success.
    duration_ms = int((time.monotonic() - t0) * 1000)
    chars_out = len(parsed_text)
    chunks_out = len(chunks)

    await repo.update_run(
        db,
        claims,
        run_id,
        status="succeeded",
        chars_out=chars_out,
        chunks_out=chunks_out,
        duration_ms=duration_ms,
    )
    await repo.update_doc_status(db, claims, doc_id, "parsed")

    _log.info(
        "ingest_document succeeded",
        extra={
            "task": "ingestion.ingest_document",
            "event": "task_completed",
            "doc_id": doc_id,
            "run_id": run_id,
            "chars_out": chars_out,
            "chunks_out": chunks_out,
            "duration_ms": duration_ms,
        },
    )
    return {"doc_id": doc_id, "run_id": run_id, "status": "succeeded"}
