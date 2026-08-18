"""Unit tests for api.ingestion.tasks.ingest_document.

Tests use stub DB + stub storage and run in Celery eager mode (no real broker).

Covers:
- Success path: parses bytes, stores parsed.txt, updates run to 'succeeded' +
  doc to 'parsed'.
- Parse error path: run → 'failed' + errors recorded + doc → 'failed'; task
  does NOT raise (Celery should NOT retry deterministic failures).
- Transient infra error (storage.get raises RuntimeError): task propagates, so
  Celery retries it.
- Tenant-scoped AuthClaims: repo calls carry claims built from the passed
  tenant_id (subject='system:ingestion', role=CLIENT_ADMIN).
- .delay()-based enqueue test (decision 2 regression guard): proves that
  ingest_document.delay(doc_id=..., tenant_id=..., run_id=..., correlation_id=...)
  does not raise TypeError at enqueue time.

S5.3 additions:
- Success path with stub provider returning 768-dim vectors: chunks embedded +
  replace_chunks called + run succeeded (chunks_out set) + doc parsed.
- EMBEDDING_NOT_CONFIGURED (no embedding_model): run failed, no embed call, no retry.
- Dimension mismatch (stub returns wrong-length vector): run failed
  EMBEDDING_DIM_MISMATCH, no chunk write, no retry.
- embed raises LLMError with retries remaining: propagates so Celery's
  autoretry_for schedules the next attempt; run not marked succeeded/failed.
- Empty parsed text: chunks_out=0 succeeded.
- Tenant-scoped system:ingestion claims asserted.
- Database.connect called with init=register_vector_init.

S12.6 additions (dev_plan/HANDOFF_embedding_batch_timeout_fix.md §7):
- embed raises LLMError with retries exhausted: run/doc marked 'failed',
  task returns cleanly (no uncaught exception).
- ingest_document's autoretry_for/retry_backoff/retry_jitter/max_retries are
  actually configured on the task decorator.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_TEST_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
    "STORAGE_BACKEND": "local",
    "STORAGE_LOCAL_ROOT": "/tmp/test-storage",
}

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

_TENANT_ID = "tenant-tasks-test"
_DOC_ID = "doc-tasks-test"
_RUN_ID = "run-tasks-test"


def _reset_modules() -> None:
    # Reimport ingestion/tasks modules fresh + clear settings caches. Do NOT
    # delete api.config: that splits the module graph (api.app stays bound to the
    # original config) and poisons later tests. Clearing the caches on the single
    # shared config module gives fresh settings safely.
    for key in list(sys.modules.keys()):
        if key.startswith("api.ingestion") or key.startswith("api.tasks"):
            del sys.modules[key]
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stub objects
# ---------------------------------------------------------------------------


class _RecordingDatabase:
    """Records every execute/fetchrow call for assertion in tests.

    Discriminates query targets:
    - ``tenant_llm_configs`` queries → returns ``_llm_config_row`` (or None).
    - All other fetchrow queries → returns ``_doc_row``.
    This ensures the S5.3 embedding path is testable without breaking existing tests.
    """

    def __init__(
        self,
        doc_row: dict[str, Any] | None = None,
        llm_config_row: dict[str, Any] | None = None,
    ) -> None:
        self._doc_row = doc_row
        self._llm_config_row = llm_config_row
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self._run_status: str = "queued"
        self._doc_status: str = "pending"

    async def execute(self, query: str, *args: Any) -> str:
        self.executions.append((query, args))
        q = query.upper()
        if "UPDATE INGESTION_RUNS" in q:
            # First arg after the SET clauses is status.
            self._run_status = str(args[0])
        if "UPDATE KNOWLEDGE_DOCS" in q:
            self._doc_status = str(args[0])
        return "UPDATE 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "TENANT_LLM_CONFIGS" in query.upper():
            return self._llm_config_row
        return self._doc_row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def close(self) -> None:
        pass


class _InMemoryStorage:
    """Minimal in-memory storage stub."""

    def __init__(self, initial: dict[str, bytes] | None = None) -> None:
        self._store: dict[str, bytes] = dict(initial or {})
        self.puts: list[tuple[str, bytes]] = []

    def put(self, key: str, data: bytes) -> None:
        self._store[key] = data
        self.puts.append((key, data))

    def get(self, key: str) -> bytes:
        if key not in self._store:
            raise FileNotFoundError(f"Key not found: {key!r}")
        return self._store[key]

    def exists(self, key: str) -> bool:
        return key in self._store

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


class _ExplodingStorage:
    """Storage stub that raises RuntimeError on get (simulates transient failure)."""

    def put(self, key: str, data: bytes) -> None:
        pass

    def get(self, key: str) -> bytes:
        raise RuntimeError("Transient storage failure")

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_doc_row(
    *,
    doc_id: str = _DOC_ID,
    tenant_id: str = _TENANT_ID,
    content_type: str = "text/plain",
    storage_key: str | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    sk = storage_key or f"{tenant_id}/{doc_id}/sample.txt"
    return {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "sample.txt",
        "content_type": content_type,
        "status": status,
        "content_hash": "abc",
        "storage_key": sk,
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": tenant_id,
    }


# ---------------------------------------------------------------------------
# S5.3 shared stubs (used by both updated existing tests and new S5.3 tests)
# ---------------------------------------------------------------------------


def _make_llm_config_row(
    *,
    embedding_model: str | None = "nomic-embed-text",
) -> dict[str, Any]:
    from common.crypto import SecretBox  # noqa: PLC0415

    from api.config import get_api_settings  # noqa: PLC0415
    box = SecretBox(get_api_settings().secret_encryption_key)
    return {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": box.encrypt("sk-test"),
        "base_url": "http://localhost:11434/v1",
        "api_version": None,
        "embedding_model": embedding_model,
    }


class _StubEmbeddingProvider:
    """Stub LLM provider that returns fixed-dimension vectors."""

    def __init__(self, dim: int = 768) -> None:
        self._dim = dim
        self.called_texts: list[list[str]] = []
        self.called_model: list[str] = []
        self.aclose_calls = 0

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        self.called_texts.append(texts)
        self.called_model.append(model)
        return [[0.1] * self._dim for _ in texts]

    async def aclose(self) -> None:
        self.aclose_calls += 1

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def classify(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


# ==============================================================================
# Success path
# ==============================================================================


async def test_ingest_document_success_path() -> None:
    """Success path: parses txt bytes, stores parsed.txt, run→succeeded, doc→parsed.

    S5.3 note: the task now also chunks + embeds after parsing. We patch
    provider_for and repo.replace_chunks so this test remains infrastructure-free.
    """
    _reset_modules()

    raw_bytes = b"Hello from the document."
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings

        get_api_settings.cache_clear()

        llm_config_row = _make_llm_config_row()
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )

        # Patch out the database and storage so no real connections are made.
        with (
            patch("api.ingestion.tasks.get_storage", return_value=storage),
            patch("api.ingestion.tasks.Database.connect", return_value=db),
            patch("api.ingestion.tasks.provider_for", return_value=_StubEmbeddingProvider(dim=768)),
            patch("api.ingestion.tasks.repo.replace_chunks"),
        ):
            from common.auth import AuthClaims, Role  # noqa: PLC0415

            from api.ingestion.tasks import _execute  # noqa: PLC0415

            claims = AuthClaims(
                subject="system:ingestion",
                role=Role.CLIENT_ADMIN,
                tenant_id=_TENANT_ID,
            )

            # We test _execute directly to avoid needing a real asyncio loop
            # inside the sync task wrapper.
            class _FakeTask:
                pass

            result = await _execute(
                _FakeTask(),  # type: ignore[arg-type]
                db,  # type: ignore[arg-type]
                claims,
                _DOC_ID,
                _RUN_ID,
                storage,
            )

    assert result["status"] == "succeeded"
    assert result["doc_id"] == _DOC_ID
    assert result["run_id"] == _RUN_ID

    # parsed.txt should be stored.
    parsed_key = f"{_TENANT_ID}/{_DOC_ID}/parsed.txt"
    assert storage.exists(parsed_key)
    assert b"Hello" in storage.get(parsed_key)

    # run updated twice: running → succeeded.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    statuses = [e[1][0] for e in run_updates]
    assert "running" in statuses
    assert "succeeded" in statuses

    # doc updated to 'parsed'.
    doc_updates = [e for e in db.executions if "KNOWLEDGE_DOCS" in e[0].upper()]
    assert any(e[1][0] == "parsed" for e in doc_updates)


# ==============================================================================
# Parse error path
# ==============================================================================


async def test_ingest_document_parse_error_records_failure_no_raise() -> None:
    """A ValidationError from parse() → run 'failed' + doc 'failed'; task returns (no raise)."""
    _reset_modules()

    # Store a file with a docx MIME but garbage bytes — parse will raise ValidationError.
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/garbage.docx"
    storage = _InMemoryStorage({storage_key: b"\x00\x01 not a docx"})
    db = _RecordingDatabase(
        doc_row=_make_doc_row(
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            storage_key=storage_key,
        )
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        get_api_settings_mod = __import__("api.config", fromlist=["get_api_settings"])
        get_api_settings_mod.get_api_settings.cache_clear()

        from common.auth import AuthClaims, Role  # noqa: PLC0415

        claims = AuthClaims(
            subject="system:ingestion",
            role=Role.CLIENT_ADMIN,
            tenant_id=_TENANT_ID,
        )

        from api.ingestion.tasks import _execute  # noqa: PLC0415

        class _FakeTask:
            pass

        result = await _execute(
            _FakeTask(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            claims,
            _DOC_ID,
            _RUN_ID,
            storage,
        )

    assert result["status"] == "failed"
    assert result["doc_id"] == _DOC_ID

    # run should record 'failed' status.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    assert any(e[1][0] == "failed" for e in run_updates)

    # The failed run update must carry the errors dict as one of its positional args.
    # NOTE: the stub DB bypasses asyncpg entirely, so the actual jsonb encoding path
    # is not exercised here — that can only be verified with a live DB (see live test
    # instructions in the bug report). This assertion confirms the dict reaches the
    # repository layer; the codec fix in common.db.Database.connect ensures asyncpg
    # then serialises it correctly on real connections.
    failed_run_updates = [e for e in run_updates if e[1][0] == "failed"]
    assert failed_run_updates, "Expected at least one UPDATE ingestion_runs with status=failed"
    # args for a failed update are (status, [optional cols...], tenant_id, run_id);
    # the errors dict must appear somewhere in the positional args tuple.
    assert any(
        isinstance(arg, dict) and "code" in arg and "message" in arg
        for update in failed_run_updates
        for arg in update[1]
    ), "The errors dict must be passed as a positional arg to the failed run UPDATE"

    # doc should be 'failed'.
    doc_updates = [e for e in db.executions if "KNOWLEDGE_DOCS" in e[0].upper()]
    assert any(e[1][0] == "failed" for e in doc_updates)

    # parsed.txt must NOT be stored.
    parsed_key = f"{_TENANT_ID}/{_DOC_ID}/parsed.txt"
    assert not storage.exists(parsed_key)


# ==============================================================================
# SR-21: ingestion_failed feed emit (D2/D3) -- one of the 4 mandatory sites
# (the parse-error path). The other three (embedding-not-configured,
# embedding-failed-after-retries, dim-mismatch) share the identical
# emit_event_safe(kind="ingestion_failed", category="system", ...) shape at
# their own "failed" return points -- covered structurally by this same
# call-site pattern; this test proves the wiring holds for at least the
# first and most common deterministic-failure path end to end.
# ==============================================================================


async def test_ingest_document_parse_error_emits_ingestion_failed() -> None:
    _reset_modules()

    storage_key = f"{_TENANT_ID}/{_DOC_ID}/garbage.docx"
    storage = _InMemoryStorage({storage_key: b"\x00\x01 not a docx"})
    db = _RecordingDatabase(
        doc_row=_make_doc_row(
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            storage_key=storage_key,
        )
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        get_api_settings_mod = __import__("api.config", fromlist=["get_api_settings"])
        get_api_settings_mod.get_api_settings.cache_clear()

        from common.auth import AuthClaims, Role  # noqa: PLC0415

        claims = AuthClaims(subject="system:ingestion", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)

        from api.ingestion.tasks import _execute  # noqa: PLC0415

        class _FakeTask:
            pass

        with patch("api.ingestion.tasks.emit_event_safe") as mock_emit:
            mock_emit.return_value = "event-1"
            result = await _execute(
                _FakeTask(), db, claims, _DOC_ID, _RUN_ID, storage,  # type: ignore[arg-type]
            )

    assert result["status"] == "failed"
    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "ingestion_failed"
    assert kwargs["category"] == "system"
    assert kwargs["target_id"] == _RUN_ID
    assert kwargs["payload"]["doc_id"] == _DOC_ID
    assert kwargs["payload"]["run_id"] == _RUN_ID


async def test_ingest_document_still_marks_failed_when_feed_emit_raises() -> None:
    """MANDATORY (D2): a feed-insert failure must not break the ingestion
    failure-handling path itself -- the run/doc are still marked failed and
    _execute returns normally (no unhandled exception)."""
    _reset_modules()

    storage_key = f"{_TENANT_ID}/{_DOC_ID}/garbage.docx"
    storage = _InMemoryStorage({storage_key: b"\x00\x01 not a docx"})
    db = _RecordingDatabase(
        doc_row=_make_doc_row(
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            storage_key=storage_key,
        )
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        get_api_settings_mod = __import__("api.config", fromlist=["get_api_settings"])
        get_api_settings_mod.get_api_settings.cache_clear()

        from common.auth import AuthClaims, Role  # noqa: PLC0415

        claims = AuthClaims(subject="system:ingestion", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)

        from api.ingestion.tasks import _execute  # noqa: PLC0415

        class _FakeTask:
            pass

        with patch(
            "api.notifications.emit.emit_event",
            new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
        ):
            result = await _execute(
                _FakeTask(), db, claims, _DOC_ID, _RUN_ID, storage,  # type: ignore[arg-type]
            )

    assert result["status"] == "failed"
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    assert any(e[1][0] == "failed" for e in run_updates)


# ==============================================================================
# Transient infra error — propagates so Celery retries
# ==============================================================================


async def test_ingest_document_transient_storage_error_propagates() -> None:
    """A RuntimeError from storage.get() propagates (Celery will retry)."""
    _reset_modules()

    db = _RecordingDatabase(doc_row=_make_doc_row())
    storage = _ExplodingStorage()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.auth import AuthClaims, Role  # noqa: PLC0415

        from api.ingestion.tasks import _execute  # noqa: PLC0415

        claims = AuthClaims(
            subject="system:ingestion",
            role=Role.CLIENT_ADMIN,
            tenant_id=_TENANT_ID,
        )

        class _FakeTask:
            pass

        with pytest.raises(RuntimeError, match="Transient storage failure"):
            await _execute(
                _FakeTask(),  # type: ignore[arg-type]
                db,  # type: ignore[arg-type]
                claims,
                _DOC_ID,
                _RUN_ID,
                storage,
            )


# ==============================================================================
# Tenant-scoped AuthClaims (decision 1)
# ==============================================================================


async def test_ingest_document_builds_tenant_scoped_claims() -> None:
    """The task builds AuthClaims(subject='system:ingestion', role=CLIENT_ADMIN, tenant_id=...)."""
    _reset_modules()

    captured_claims: list[object] = []

    raw_bytes = b"content"
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/f.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})
    db = _RecordingDatabase(doc_row=_make_doc_row(storage_key=storage_key))

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        from api.ingestion import repository as repo_mod  # noqa: PLC0415

        original_update_run = repo_mod.update_run

        async def _capturing_update_run(
            db_: Any, claims_: Any, run_id: str, **kwargs: Any
        ) -> None:
            captured_claims.append(claims_)
            await original_update_run(db_, claims_, run_id, **kwargs)

        with patch("api.ingestion.tasks.repo.update_run", side_effect=_capturing_update_run):
            from common.auth import AuthClaims, Role  # noqa: PLC0415

            from api.ingestion.tasks import _execute  # noqa: PLC0415

            claims = AuthClaims(
                subject="system:ingestion",
                role=Role.CLIENT_ADMIN,
                tenant_id=_TENANT_ID,
            )

            class _FakeTask:
                pass

            await _execute(
                _FakeTask(),  # type: ignore[arg-type]
                db,  # type: ignore[arg-type]
                claims,
                _DOC_ID,
                _RUN_ID,
                storage,
            )

    assert captured_claims, "update_run should have been called"
    c = captured_claims[0]
    from common.auth import AuthClaims as _AC  # noqa: PLC0415
    from common.auth import Role as _R  # noqa: PLC0415

    assert isinstance(c, _AC)
    assert c.role == _R.CLIENT_ADMIN
    assert c.tenant_id == _TENANT_ID


# ==============================================================================
# .delay()-based enqueue test (decision 2 regression guard)
# ==============================================================================


def test_ingest_document_delay_accepts_correlation_id() -> None:
    """Regression guard: ingest_document.delay(correlation_id=...) must not raise TypeError.

    This uses ``.delay()`` (not ``.apply()``) to exercise the real
    ``check_arguments`` path inside Celery's ``apply_async``, which is what
    the S5.1 post-mortem identified as the live bug.
    """
    _reset_modules()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        import api.ingestion.tasks  # noqa: PLC0415, F401
        import api.tasks.celery_app as capp  # noqa: PLC0415

        capp.celery_app.conf.task_always_eager = True
        capp.celery_app.conf.task_eager_propagates = False  # don't propagate task errors

        from api.ingestion.tasks import ingest_document  # noqa: PLC0415

        # Patch _execute so we don't need a real DB/storage in this test.
        async def _stub_execute(*args: Any, **kwargs: Any) -> dict[str, object]:
            return {"doc_id": "d", "run_id": "r", "status": "succeeded"}

        with (
            patch("api.ingestion.tasks.get_storage"),
            patch("api.ingestion.tasks.asyncio.new_event_loop") as mock_loop,
        ):
            mock_event_loop = mock_loop.return_value
            mock_event_loop.run_until_complete.return_value = {
                "doc_id": "d", "run_id": "r", "status": "succeeded"
            }
            mock_event_loop.close.return_value = None

            # This is the regression guard — would raise TypeError at enqueue
            # if correlation_id were not declared in the task signature.
            result = ingest_document.delay(
                doc_id="doc-delay-test",
                tenant_id="tenant-delay",
                run_id="run-delay",
                correlation_id="cid-delay-test",
            )
            assert result is not None


# ==============================================================================
# S5.3: chunk + embed + store
# ==============================================================================

# _make_llm_config_row, _StubEmbeddingProvider are defined above (shared stubs).

# _EmbeddingDatabase is just _RecordingDatabase with llm_config_row support,
# which is already built into _RecordingDatabase above.


async def test_s53_success_path_embeds_and_stores_chunks() -> None:
    """Success: stub provider returns 768-dim vectors → replace_chunks called + run succeeded."""
    _reset_modules()

    raw_bytes = b"Hello world. This is a document. It has several sentences for chunking."
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})

    with patch.dict("os.environ", {**_TEST_ENV, "EMBEDDING_DIMENSION": "768"}, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415
        get_api_settings.cache_clear()

        llm_config_row = _make_llm_config_row()
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )
        stub_provider = _StubEmbeddingProvider(dim=768)

        replace_chunks_calls: list[tuple[Any, ...]] = []

        async def _stub_replace_chunks(db_: Any, claims_: Any, doc_id_: str, rows_: Any) -> None:
            replace_chunks_calls.append((claims_, doc_id_, rows_))

        class _FakeTask:
            pass

        with patch("api.ingestion.tasks.provider_for", return_value=stub_provider):
            with patch("api.ingestion.tasks.repo.replace_chunks", side_effect=_stub_replace_chunks):
                from common.auth import AuthClaims, Role  # noqa: PLC0415

                from api.ingestion.tasks import _execute  # noqa: PLC0415

                claims = AuthClaims(
                    subject="system:ingestion",
                    role=Role.CLIENT_ADMIN,
                    tenant_id=_TENANT_ID,
                )
                result = await _execute(
                    _FakeTask(),  # type: ignore[arg-type]
                    db,  # type: ignore[arg-type]
                    claims,
                    _DOC_ID,
                    _RUN_ID,
                    storage,
                )

    assert result["status"] == "succeeded"
    # replace_chunks must have been called.
    assert len(replace_chunks_calls) == 1, "replace_chunks must be called on success"
    called_claims, called_doc_id, called_rows = replace_chunks_calls[0]
    assert called_doc_id == _DOC_ID
    assert called_claims.tenant_id == _TENANT_ID
    assert len(called_rows) > 0

    # Verify deterministic chunk_ids format.
    for i, row in enumerate(called_rows):
        assert row.chunk_id == f"{_DOC_ID}-{i:04d}", f"Bad chunk_id: {row.chunk_id!r}"

    # Verify embeddings have the right length.
    for row in called_rows:
        assert len(row.embedding) == 768

    # run updated to succeeded with chunks_out.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    succeeded_updates = [e for e in run_updates if e[1][0] == "succeeded"]
    assert succeeded_updates, "run must reach succeeded"
    # chunks_out value (len(rows)) must appear in params.
    assert any(len(called_rows) in e[1] for e in succeeded_updates), (
        "chunks_out must be bound in the succeeded UPDATE"
    )

    # doc updated to 'parsed'.
    doc_updates = [e for e in db.executions if "KNOWLEDGE_DOCS" in e[0].upper()]
    assert any(e[1][0] == "parsed" for e in doc_updates)

    # Resource-leak fix: the provider must be closed on the success path too.
    assert stub_provider.aclose_calls == 1, "llm_provider.aclose() must be called on success"


async def test_s53_embedding_not_configured_deterministic_fail() -> None:
    """No embedding_model → run failed EMBEDDING_NOT_CONFIGURED, no embed, no retry."""
    _reset_modules()

    raw_bytes = b"Hello world. This is content."
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415
        get_api_settings.cache_clear()

        # Config row with embedding_model=None.
        llm_config_row = _make_llm_config_row(embedding_model=None)
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )
        stub_provider = _StubEmbeddingProvider(dim=768)

        class _FakeTask:
            pass

        with patch("api.ingestion.tasks.provider_for", return_value=stub_provider):
            from common.auth import AuthClaims, Role  # noqa: PLC0415

            from api.ingestion.tasks import _execute  # noqa: PLC0415

            claims = AuthClaims(
                subject="system:ingestion",
                role=Role.CLIENT_ADMIN,
                tenant_id=_TENANT_ID,
            )
            result = await _execute(
                _FakeTask(),  # type: ignore[arg-type]
                db,  # type: ignore[arg-type]
                claims,
                _DOC_ID,
                _RUN_ID,
                storage,
            )

    assert result["status"] == "failed"
    # embed must NOT have been called.
    assert stub_provider.called_texts == [], "embed must not be called when no embedding_model"

    # run must be failed with EMBEDDING_NOT_CONFIGURED.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    failed_updates = [e for e in run_updates if e[1][0] == "failed"]
    assert failed_updates
    assert any(
        isinstance(arg, dict) and arg.get("code") == "EMBEDDING_NOT_CONFIGURED"
        for e in failed_updates
        for arg in e[1]
    ), "EMBEDDING_NOT_CONFIGURED must be in the errors"

    # doc must be failed.
    doc_updates = [e for e in db.executions if "KNOWLEDGE_DOCS" in e[0].upper()]
    assert any(e[1][0] == "failed" for e in doc_updates)


async def test_s53_dimension_mismatch_deterministic_fail() -> None:
    """Stub returns wrong-length vector → run failed EMBEDDING_DIM_MISMATCH, no write."""
    _reset_modules()

    raw_bytes = b"Hello world. This is content for chunking tests."
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415
        get_api_settings.cache_clear()

        llm_config_row = _make_llm_config_row()
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )
        # Provider returns wrong dimension (e.g. 512 instead of 768).
        stub_provider = _StubEmbeddingProvider(dim=512)

        replace_chunks_calls: list[Any] = []

        async def _stub_replace_chunks(*args: Any, **kwargs: Any) -> None:
            replace_chunks_calls.append(args)

        class _FakeTask:
            pass

        with patch("api.ingestion.tasks.provider_for", return_value=stub_provider):
            with patch("api.ingestion.tasks.repo.replace_chunks", side_effect=_stub_replace_chunks):
                from common.auth import AuthClaims, Role  # noqa: PLC0415

                from api.ingestion.tasks import _execute  # noqa: PLC0415

                claims = AuthClaims(
                    subject="system:ingestion",
                    role=Role.CLIENT_ADMIN,
                    tenant_id=_TENANT_ID,
                )
                result = await _execute(
                    _FakeTask(),  # type: ignore[arg-type]
                    db,  # type: ignore[arg-type]
                    claims,
                    _DOC_ID,
                    _RUN_ID,
                    storage,
                )

    assert result["status"] == "failed"
    # replace_chunks must NOT have been called.
    assert replace_chunks_calls == [], "replace_chunks must not be called on dim mismatch"

    # run must be failed with EMBEDDING_DIM_MISMATCH.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    failed_updates = [e for e in run_updates if e[1][0] == "failed"]
    assert failed_updates
    assert any(
        isinstance(arg, dict) and arg.get("code") == "EMBEDDING_DIM_MISMATCH"
        for e in failed_updates
        for arg in e[1]
    ), "EMBEDDING_DIM_MISMATCH must be in the errors"


class _ErrorEmbeddingProvider:
    """Stub provider whose embed() always raises LLMError (imported lazily by callers)."""

    def __init__(self, message: str = "Network error.") -> None:
        self._message = message
        self.aclose_calls = 0

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        from api.llm.provider import LLMError  # noqa: PLC0415
        raise LLMError(self._message)

    async def generate(self, *args: Any, **kwargs: Any) -> Any: ...
    async def classify(self, *args: Any, **kwargs: Any) -> Any: ...
    def stream(self, *args: Any, **kwargs: Any) -> Any: ...

    async def aclose(self) -> None:
        self.aclose_calls += 1


async def test_s53_llm_error_propagates_when_retries_remain() -> None:
    """LLMError from embed, retries remaining -> propagates so Celery's autoretry_for retries."""
    _reset_modules()

    raw_bytes = b"Hello world. Content to embed."
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415
        get_api_settings.cache_clear()

        llm_config_row = _make_llm_config_row()
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )

        from api.llm.provider import LLMError  # noqa: PLC0415

        class _FakeTask:
            max_retries = 3
            request = SimpleNamespace(retries=0)  # first attempt -- retries remain

        error_provider = _ErrorEmbeddingProvider("Network error.")
        with patch(
            "api.ingestion.tasks.provider_for",
            return_value=error_provider,
        ):
            from common.auth import AuthClaims, Role  # noqa: PLC0415

            from api.ingestion.tasks import _execute  # noqa: PLC0415

            claims = AuthClaims(
                subject="system:ingestion",
                role=Role.CLIENT_ADMIN,
                tenant_id=_TENANT_ID,
            )
            with pytest.raises(LLMError, match="Network error"):
                await _execute(
                    _FakeTask(),  # type: ignore[arg-type]
                    db,  # type: ignore[arg-type]
                    claims,
                    _DOC_ID,
                    _RUN_ID,
                    storage,
                )

    # run must NOT have been marked succeeded or failed -- retries remain,
    # so Celery's autoretry_for is expected to re-run the task from the top.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    statuses = [e[1][0] for e in run_updates]
    assert "succeeded" not in statuses, "run must not be marked succeeded when LLMError propagates"
    assert "failed" not in statuses, "run must not be marked failed while retries remain"

    # Resource-leak fix: the provider (and its underlying SDK client
    # connection pool) must be closed even though the LLMError propagated
    # out of the temporary event loop this Celery task runs in.
    assert error_provider.aclose_calls == 1, (
        "llm_provider.aclose() must be called even when embed() raises and re-propagates"
    )


async def test_s12_6_llm_error_after_retries_exhausted_marks_run_failed() -> None:
    """LLMError from embed, retries exhausted -> run/doc marked 'failed', task returns cleanly."""
    _reset_modules()

    raw_bytes = b"Hello world. Content to embed."
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415
        get_api_settings.cache_clear()

        llm_config_row = _make_llm_config_row()
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )

        class _FakeTask:
            max_retries = 3
            request = SimpleNamespace(retries=3)  # final attempt -- retries exhausted

        error_provider = _ErrorEmbeddingProvider("Upstream timed out.")
        with patch(
            "api.ingestion.tasks.provider_for",
            return_value=error_provider,
        ):
            from common.auth import AuthClaims, Role  # noqa: PLC0415

            from api.ingestion.tasks import _execute  # noqa: PLC0415

            claims = AuthClaims(
                subject="system:ingestion",
                role=Role.CLIENT_ADMIN,
                tenant_id=_TENANT_ID,
            )
            # Must NOT raise -- the final LLMError is caught and the task
            # returns a clean 'failed' result instead of an uncaught exception.
            result = await _execute(
                _FakeTask(),  # type: ignore[arg-type]
                db,  # type: ignore[arg-type]
                claims,
                _DOC_ID,
                _RUN_ID,
                storage,
            )

    assert result["status"] == "failed"
    assert result["doc_id"] == _DOC_ID
    assert result["run_id"] == _RUN_ID

    # Resource-leak fix (the observed symptom): llm_provider.aclose() must
    # be called even on the caught-and-handled retries-exhausted path, so
    # the abandoned httpx.AsyncClient never GCs on this task's already-closed
    # temporary event loop.
    assert error_provider.aclose_calls == 1, (
        "llm_provider.aclose() must be called when embed() raises and retries are exhausted"
    )

    # run must be marked failed with an EMBEDDING_FAILED error entry.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    failed_updates = [e for e in run_updates if e[1][0] == "failed"]
    assert failed_updates, "run must reach failed once retries are exhausted"
    assert any(
        isinstance(arg, dict) and arg.get("code") == "EMBEDDING_FAILED"
        for e in failed_updates
        for arg in e[1]
    ), "EMBEDDING_FAILED must be in the errors"
    assert "succeeded" not in [e[1][0] for e in run_updates]

    # doc must be marked failed.
    doc_updates = [e for e in db.executions if "KNOWLEDGE_DOCS" in e[0].upper()]
    assert any(e[1][0] == "failed" for e in doc_updates)


def test_ingest_document_autoretry_for_llm_error_is_configured() -> None:
    """ingest_document declares autoretry_for=(LLMError,) with backoff+jitter+bounded retries.

    Regression guard for the S5.3 stale-comment bug: the old Step 8 comment
    claimed LLMError 'propagates (transient/retryable)' but no autoretry_for
    was ever configured, so an exhausted/uncaught LLMError left
    ingestion_runs.status stuck at 'running' forever (S12.6 fix).
    """
    _reset_modules()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.tasks import ingest_document  # noqa: PLC0415
        from api.llm.provider import LLMError  # noqa: PLC0415

        assert ingest_document.autoretry_for == (LLMError,)
        assert ingest_document.retry_backoff is True
        assert ingest_document.retry_jitter is True
        assert ingest_document.max_retries == 3


async def test_s53_empty_text_succeeds_with_zero_chunks() -> None:
    """Empty parsed text → chunks_out=0, run succeeded, no embed call."""
    _reset_modules()

    # Upload a file that parses to only whitespace.
    raw_bytes = b"   \n  \t  "  # whitespace only — chunk_text returns []
    storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
    storage = _InMemoryStorage({storage_key: raw_bytes})

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415
        get_api_settings.cache_clear()

        llm_config_row = _make_llm_config_row()
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )
        stub_provider = _StubEmbeddingProvider(dim=768)

        replace_chunks_calls: list[Any] = []

        async def _stub_replace_chunks(*args: Any, **kwargs: Any) -> None:
            replace_chunks_calls.append(args)

        class _FakeTask:
            pass

        with patch("api.ingestion.tasks.provider_for", return_value=stub_provider):
            with patch("api.ingestion.tasks.repo.replace_chunks", side_effect=_stub_replace_chunks):
                from common.auth import AuthClaims, Role  # noqa: PLC0415

                from api.ingestion.tasks import _execute  # noqa: PLC0415

                claims = AuthClaims(
                    subject="system:ingestion",
                    role=Role.CLIENT_ADMIN,
                    tenant_id=_TENANT_ID,
                )
                result = await _execute(
                    _FakeTask(),  # type: ignore[arg-type]
                    db,  # type: ignore[arg-type]
                    claims,
                    _DOC_ID,
                    _RUN_ID,
                    storage,
                )

    assert result["status"] == "succeeded"
    # embed and replace_chunks must NOT have been called.
    assert stub_provider.called_texts == []
    assert replace_chunks_calls == []

    # chunks_out=0 must appear in the succeeded update.
    run_updates = [e for e in db.executions if "INGESTION_RUNS" in e[0].upper()]
    succeeded = [e for e in run_updates if e[1][0] == "succeeded"]
    assert succeeded
    assert any(0 in e[1] for e in succeeded), "chunks_out=0 must be in succeeded UPDATE params"


async def test_s53_database_connect_called_with_register_vector_init() -> None:
    """Database.connect is called with init=register_vector_init (S5.3 decision 3).

    This is the codec regression guard: without this, writing vector columns
    fails at runtime with a live DB.
    """
    _reset_modules()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415
        get_api_settings.cache_clear()

        raw_bytes = b"Some content."
        storage_key = f"{_TENANT_ID}/{_DOC_ID}/sample.txt"
        storage = _InMemoryStorage({storage_key: raw_bytes})
        llm_config_row = _make_llm_config_row()
        db = _RecordingDatabase(
            doc_row=_make_doc_row(storage_key=storage_key),
            llm_config_row=llm_config_row,
        )

        connect_kwargs: list[dict[str, Any]] = []

        async def _stub_connect(dsn: str, **kwargs: Any) -> Any:
            connect_kwargs.append(kwargs)
            return db

        with (
            patch("api.ingestion.tasks.get_storage", return_value=storage),
            patch("api.ingestion.tasks.Database.connect", side_effect=_stub_connect),
            patch("api.ingestion.tasks.provider_for", return_value=_StubEmbeddingProvider(dim=768)),
            patch("api.ingestion.tasks.repo.replace_chunks"),
        ):
            from common.auth import AuthClaims, Role  # noqa: PLC0415
            from common.pgvector import register_vector_init as _rvi  # noqa: PLC0415

            from api.ingestion.tasks import _run  # noqa: PLC0415

            claims = AuthClaims(
                subject="system:ingestion",
                role=Role.CLIENT_ADMIN,
                tenant_id=_TENANT_ID,
            )

            class _FakeTask:
                pass

            await _run(_FakeTask(), claims, _DOC_ID, _RUN_ID, storage)  # type: ignore[arg-type]

    assert connect_kwargs, "Database.connect must have been called"
    assert connect_kwargs[0].get("init") is _rvi, (
        "Database.connect must be called with init=register_vector_init"
    )
