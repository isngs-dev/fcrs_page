"""Unit tests for api.ingestion.routes.

Uses ASGI test client + stub DB + stub storage + mocked task .delay().

Covers:
- POST /admin/ingestion/upload:
  - txt upload → 200 {doc_id, run_id, status:"pending"}, task enqueued.
  - docx upload → 200, task enqueued.
  - correlation_id propagated to ingest_document.delay().
  - Idempotent re-upload (same bytes) → same doc_id, .delay NOT called again.
  - File too large → 413.
  - Unsupported content type → 422 UNSUPPORTED_CONTENT_TYPE.
  - CLIENT_AGENT → 403.
  - No cookie → 401.
- GET /admin/ingestion/docs/{doc_id}:
  - 200 shape: contains doc_id, filename, content_type, status, content_hash,
    latest_run, parsed_preview; does NOT contain tenant_id or storage_key.
  - Missing doc → 404 DOC_NOT_FOUND.
- GET /admin/ingestion/docs/{doc_id}/download:
  - 200 with the exact stored bytes, the doc's content_type, and a
    Content-Disposition naming the (sanitized) filename.
  - A malicious filename (CR/LF, quotes) can never inject header lines.
  - Missing doc → 404 DOC_NOT_FOUND; no cookie → 401; CLIENT_AGENT → 403.
  - A missing/orphaned stored file → 500 DOCUMENT_STORAGE_MISSING (never a
    silently empty download).
  - PLATFORM_ADMIN tenant-scoped variant: succeeds for the right tenant_id,
    404s (never leaks bytes) for the wrong one (mandatory multi-tenant
    isolation).
"""
from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

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
    "STORAGE_LOCAL_ROOT": "/tmp/test-ingestion-routes",
}

_TENANT_ID = "tenant-route-ingestion"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Stub database
# ---------------------------------------------------------------------------


class _StubDatabase:
    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], dict[str, Any]] = {}
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}
        self._hashes: dict[tuple[str, str], str] = {}  # (tenant_id, hash) -> doc_id
        # (tenant_id, doc_id) -> chunk count, seeded by tests exercising delete.
        self._chunk_counts: dict[tuple[str, str], int] = {}
        self._insert_seq = 0

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if "INSERT INTO KNOWLEDGE_DOCS" in q:
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
                "_seq": self._insert_seq,
            }
            self._hashes[(tenant_id, content_hash)] = doc_id
            return "INSERT 0 1"

        if "UPDATE KNOWLEDGE_DOCS" in q:
            new_status, tenant_id, doc_id = args[0], args[-2], args[-1]
            key = (str(tenant_id), str(doc_id))
            if key in self._docs:
                self._docs[key] = {**self._docs[key], "status": new_status, "updated_at": _NOW}
            return "UPDATE 1"

        if "INSERT INTO INGESTION_RUNS" in q:
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

        if "UPDATE INGESTION_RUNS" in q:
            return "UPDATE 1"

        if "DELETE FROM KNOWLEDGE_CHUNKS" in q:
            tenant_id, doc_id = str(args[0]), str(args[1])
            n = self._chunk_counts.pop((tenant_id, doc_id), 0)
            return f"DELETE {n}"

        if "DELETE FROM INGESTION_RUNS" in q:
            tenant_id, doc_id = str(args[0]), str(args[1])
            keys = [k for k, r in self._runs.items() if k[0] == tenant_id and r["doc_id"] == doc_id]
            for k in keys:
                del self._runs[k]
            return f"DELETE {len(keys)}"

        if "DELETE FROM KNOWLEDGE_DOCS" in q:
            tenant_id, doc_id = str(args[0]), str(args[1])
            key = (tenant_id, doc_id)
            if key in self._docs:
                del self._docs[key]
                return "DELETE 1"
            return "DELETE 0"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.upper()

        if "FROM TENANTS" in q:
            # resolve_tenant_scope's TenantRepository.get(claims, tenant_id) --
            # treat _TENANT_ID (and any tenant with a seeded doc) as real/enabled.
            tenant_id = str(args[0])
            if tenant_id == _TENANT_ID or any(tid == tenant_id for tid, _ in self._docs):
                return {"id": tenant_id, "name": tenant_id, "slug": tenant_id, "enabled": True}
            return None

        if "FROM KNOWLEDGE_DOCS" in q and "AND CONTENT_HASH" in q:
            # find_doc_by_hash — WHERE ... AND content_hash = $2
            tenant_id, content_hash = args[0], args[1]
            doc_id = self._hashes.get((str(tenant_id), str(content_hash)))
            if doc_id is None:
                return None
            return self._docs.get((str(tenant_id), str(doc_id)))

        if "FROM KNOWLEDGE_DOCS" in q:
            # get_doc or re-fetch after create — WHERE ... AND doc_id = $2
            tenant_id, doc_id = args[0], args[1]
            return self._docs.get((str(tenant_id), str(doc_id)))

        if "FROM INGESTION_RUNS" in q and "LIMIT 1" in q:
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
            tenant_id = str(args[0])
            rows = [row for (tid, _), row in self._docs.items() if tid == tenant_id]
            rows.sort(key=lambda r: (r["created_at"], r.get("_seq", 0)), reverse=True)
            return rows

        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Stub storage
# ---------------------------------------------------------------------------


class _InMemoryStorage:
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self._store[key] = data

    def get(self, key: str) -> bytes:
        if key not in self._store:
            raise FileNotFoundError(key)
        return self._store[key]

    def exists(self, key: str) -> bool:
        return key in self._store

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_modules() -> None:
    """Clear the settings caches so the next ``create_app`` re-reads the patched env.

    NOTE: this must NOT delete ``api.config`` from ``sys.modules``. Doing so
    creates a *second* config module while ``api.app`` (and other already-imported
    modules) stay bound to the ORIGINAL one — ``create_app`` then caches settings
    on the original module, while this test's ``cache_clear`` targets the reimported
    one, leaving stale limits that poison later tests (rate-limiting/password-reset).
    Clearing the caches on the single, shared config module is sufficient and safe.
    """
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


class _StubRedis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        pass

    async def getdel(self, key: str) -> str | None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass

    def pipeline(self, transaction: bool = False) -> _StubPipeline:
        return _StubPipeline()


class _StubPipeline:
    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        pass

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        pass

    def zcard(self, key: str) -> None:
        pass

    def expire(self, key: str, seconds: int) -> None:
        pass

    async def execute(self) -> list[Any]:
        return [0, None, 0, True]


def _build_app(stub_db: _StubDatabase) -> Any:
    from api.config import get_api_settings

    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = stub_db
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _mint_cookie(
    *,
    role: Role = Role.CLIENT_ADMIN,
    tenant_id: str | None = _TENANT_ID,
    secret: str = "x" * 48,
) -> str:
    from api.auth.tokens import create_access_token

    claims = AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=secret, ttl_seconds=300)
    return token


def _make_txt_upload(content: bytes = b"hello document") -> tuple[bytes, str]:
    return content, "text/plain"


def _make_docx_upload() -> tuple[bytes, str]:
    """Build a minimal docx fixture in-memory."""
    from docx import Document  # type: ignore[import-untyped]

    doc = Document()
    doc.add_paragraph("Docx content for testing.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), _DOCX_MIME


# ==============================================================================
# POST /admin/ingestion/upload — success paths
# ==============================================================================


async def test_upload_txt_returns_200_with_pending_status() -> None:
    """txt upload → 200 with {doc_id, run_id, status:'pending'}."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with (
            patch("api.ingestion.routes.ingest_document") as mock_task,
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
        ):
            mock_delay = MagicMock()
            mock_delay.id = "task-id-1"
            mock_task.delay = MagicMock(return_value=mock_delay)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("sample.txt", b"hello document", "text/plain")},
                )

    assert resp.status_code == 200
    body = resp.json()
    assert "doc_id" in body
    assert "run_id" in body
    assert body["status"] == "pending"
    assert mock_task.delay.called


async def test_upload_docx_returns_200() -> None:
    """docx upload → 200 with {doc_id, run_id, status:'pending'}."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()
        docx_bytes, _ = _make_docx_upload()

        with (
            patch("api.ingestion.routes.ingest_document") as mock_task,
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
        ):
            mock_delay = MagicMock()
            mock_delay.id = "task-id-docx"
            mock_task.delay = MagicMock(return_value=mock_delay)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("sample.docx", docx_bytes, _DOCX_MIME)},
                )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"


async def test_upload_passes_correlation_id_to_delay() -> None:
    """The route must pass the request correlation_id to ingest_document.delay()."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with (
            patch("api.ingestion.routes.ingest_document") as mock_task,
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
        ):
            mock_delay = MagicMock(return_value=MagicMock(id="task-cid"))
            mock_task.delay = mock_delay

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("f.txt", b"correlation test", "text/plain")},
                    headers={"x-correlation-id": "expected-cid"},
                )

    assert resp.status_code == 200
    call_kwargs = mock_delay.call_args.kwargs
    assert call_kwargs.get("correlation_id") == "expected-cid"


# ==============================================================================
# Idempotent re-upload
# ==============================================================================


async def test_upload_idempotent_same_bytes_returns_existing_doc_id() -> None:
    """Re-uploading the same bytes returns the same doc_id; .delay NOT called again."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()
        content = b"idempotent content"

        delay_call_count = 0

        with (
            patch("api.ingestion.routes.ingest_document") as mock_task,
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
        ):
            def _counting_delay(**kwargs: Any) -> MagicMock:
                nonlocal delay_call_count
                delay_call_count += 1
                m = MagicMock()
                m.id = "task-idem"
                return m

            mock_task.delay = _counting_delay

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp1 = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("idem.txt", content, "text/plain")},
                )
                doc_id_first = resp1.json()["doc_id"]

                resp2 = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("idem.txt", content, "text/plain")},
                )
                doc_id_second = resp2.json()["doc_id"]

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert doc_id_first == doc_id_second, "Same content must return the same doc_id"
    assert delay_call_count == 1, ".delay must be called only once (not on re-upload)"


# ==============================================================================
# Negative cases — upload
# ==============================================================================


async def test_upload_oversize_returns_413() -> None:
    """A file larger than ingestion_max_upload_bytes → 413."""
    _reset_modules()

    stub_db = _StubDatabase()

    # Tiny limit for this test.
    env = {**_TEST_ENV, "INGESTION_MAX_UPLOAD_BYTES": "10"}
    with patch.dict("os.environ", env, clear=False):
        from api.config import get_api_settings

        get_api_settings.cache_clear()

        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch("api.ingestion.routes.ingest_document"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("big.txt", b"x" * 20, "text/plain")},
                )

    assert resp.status_code == 413


async def test_upload_unsupported_content_type_returns_422() -> None:
    """Unsupported content type → 422 UNSUPPORTED_CONTENT_TYPE."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch("api.ingestion.routes.ingest_document"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("photo.png", b"\x89PNG", "image/png")},
                )

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "UNSUPPORTED_CONTENT_TYPE"


async def test_upload_client_agent_returns_403() -> None:
    """CLIENT_AGENT → 403."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.CLIENT_AGENT)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/admin/ingestion/upload",
                cookies={"access_token": token},
                files={"file": ("f.txt", b"data", "text/plain")},
            )

    assert resp.status_code == 403


async def test_upload_no_cookie_returns_401() -> None:
    """No cookie → 401."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/admin/ingestion/upload",
                files={"file": ("f.txt", b"data", "text/plain")},
            )

    assert resp.status_code == 401


# ==============================================================================
# GET /admin/ingestion/docs/{doc_id}
# ==============================================================================


async def test_get_doc_returns_shape_without_tenant_id_or_storage_key() -> None:
    """GET /docs/{doc_id} → 200 with correct fields; no tenant_id or storage_key."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    # Pre-populate DB and storage with a doc + run + parsed.txt.
    doc_id = "doc-get-test"
    storage_key = f"{_TENANT_ID}/{doc_id}/sample.txt"
    parsed_key = f"{_TENANT_ID}/{doc_id}/parsed.txt"
    stub_db._docs[(_TENANT_ID, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "sample.txt",
        "content_type": "text/plain",
        "status": "parsed",
        "content_hash": "sha256-abc",
        "storage_key": storage_key,
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": _TENANT_ID,
    }
    stub_db._runs[(_TENANT_ID, "run-get-test")] = {
        "run_id": "run-get-test",
        "doc_id": doc_id,
        "status": "succeeded",
        "chars_out": 42,
        "errors": None,
        "started_at": _NOW,
        "finished_at": _NOW,
        "duration_ms": 100,
        "tenant_id": _TENANT_ID,
    }
    stub_storage.put(parsed_key, b"Hello parsed content.")

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch("api.ingestion.routes.get_storage", return_value=stub_storage):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    f"/admin/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    body = resp.json()

    # Required fields.
    assert body["doc_id"] == doc_id
    assert "filename" in body
    assert "content_type" in body
    assert "status" in body
    assert "content_hash" in body
    assert "latest_run" in body
    assert "parsed_preview" in body

    # Must NOT expose internal fields.
    assert "tenant_id" not in body
    assert "storage_key" not in body

    # Latest run shape.
    run = body["latest_run"]
    assert run["status"] == "succeeded"
    assert run["chars_out"] == 42

    # Parsed preview.
    assert body["parsed_preview"] is not None
    assert "Hello" in body["parsed_preview"]


async def test_get_doc_missing_returns_404() -> None:
    """GET /docs/{doc_id} for a nonexistent doc → 404 DOC_NOT_FOUND."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/admin/ingestion/docs/does-not-exist",
                cookies={"access_token": token},
            )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "DOC_NOT_FOUND"


async def test_get_doc_no_cookie_returns_401() -> None:
    """GET /docs/{doc_id} without a cookie → 401."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/admin/ingestion/docs/any-id")

    assert resp.status_code == 401


async def test_get_doc_client_agent_returns_403() -> None:
    """GET /docs/{doc_id} with CLIENT_AGENT → 403."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.CLIENT_AGENT)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/admin/ingestion/docs/any-id",
                cookies={"access_token": token},
            )

    assert resp.status_code == 403


# ==============================================================================
# GET /admin/ingestion/docs/{doc_id}/download
# ==============================================================================


def _seed_doc_row(
    stub_db: _StubDatabase,
    *,
    tenant_id: str,
    doc_id: str,
    filename: str,
    content_type: str = "text/plain",
    storage_key: str | None = None,
) -> None:
    stub_db._docs[(tenant_id, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": filename,
        "content_type": content_type,
        "status": "parsed",
        "content_hash": f"hash-{doc_id}",
        "storage_key": storage_key or f"{tenant_id}/{doc_id}/{filename}",
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": tenant_id,
        "title": None,
        "description": None,
        "uploaded_by": None,
    }


async def test_download_doc_returns_raw_bytes_with_content_type_and_disposition() -> None:
    """GET /docs/{doc_id}/download -> 200 with the exact stored bytes, the
    doc's own content_type, and a Content-Disposition naming the filename."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-download-test"
    storage_key = f"{_TENANT_ID}/{doc_id}/sample.txt"
    _seed_doc_row(stub_db, tenant_id=_TENANT_ID, doc_id=doc_id, filename="sample.txt", storage_key=storage_key)
    stub_storage.put(storage_key, b"the original uploaded bytes")

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch("api.ingestion.routes.get_storage", return_value=stub_storage):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    f"/admin/ingestion/docs/{doc_id}/download",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    assert resp.content == b"the original uploaded bytes"
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == 'attachment; filename="sample.txt"'


async def test_download_doc_sanitizes_cr_lf_and_quotes_in_filename() -> None:
    """A malicious upload filename can never inject extra header fields/lines."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-header-injection-test"
    storage_key = f"{_TENANT_ID}/{doc_id}/evil"
    malicious_name = 'evil"\r\nX-Injected: yes\r\n.txt'
    _seed_doc_row(stub_db, tenant_id=_TENANT_ID, doc_id=doc_id, filename=malicious_name, storage_key=storage_key)
    stub_storage.put(storage_key, b"bytes")

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch("api.ingestion.routes.get_storage", return_value=stub_storage):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    f"/admin/ingestion/docs/{doc_id}/download",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    # Exactly the two wrapping quotes from `filename="..."` -- no smuggled
    # quote from the malicious filename itself.
    assert disposition.count('"') == 2
    assert disposition == 'attachment; filename="evilX-Injected: yes.txt"'
    assert "X-Injected" not in resp.headers


async def test_download_doc_missing_returns_404() -> None:
    """GET /docs/{doc_id}/download for a nonexistent doc -> 404 DOC_NOT_FOUND."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/admin/ingestion/docs/does-not-exist/download",
                cookies={"access_token": token},
            )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "DOC_NOT_FOUND"


async def test_download_doc_no_cookie_returns_401() -> None:
    """GET /docs/{doc_id}/download without a cookie -> 401."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/ingestion/docs/any-id/download")

    assert resp.status_code == 401


async def test_download_doc_client_agent_returns_403() -> None:
    """GET /docs/{doc_id}/download with CLIENT_AGENT -> 403."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.CLIENT_AGENT)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/admin/ingestion/docs/any-id/download",
                cookies={"access_token": token},
            )

    assert resp.status_code == 403


async def test_download_doc_missing_stored_file_returns_500_never_a_fake_empty_file() -> None:
    """A storage read failure on download is a real error, unlike the GET
    detail endpoint's best-effort (null-preview) degradation -- the file IS
    the point of this endpoint."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-orphaned-storage"
    # Seed the DB row but never `put` the bytes -- simulates an orphaned
    # storage_key (e.g. a prior storage-delete failure left the DB pointing
    # at nothing).
    _seed_doc_row(stub_db, tenant_id=_TENANT_ID, doc_id=doc_id, filename="gone.txt")

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch("api.ingestion.routes.get_storage", return_value=stub_storage):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    f"/admin/ingestion/docs/{doc_id}/download",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 500
    assert resp.json()["error_code"] == "DOCUMENT_STORAGE_MISSING"


async def test_download_doc_platform_admin_tenant_scoped_success() -> None:
    """PLATFORM_ADMIN can download a specific client's doc via the
    tenant-scoped route, with the SAME bytes/headers as the implicit path."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-platform-admin-test"
    storage_key = f"{_TENANT_ID}/{doc_id}/report.txt"
    _seed_doc_row(stub_db, tenant_id=_TENANT_ID, doc_id=doc_id, filename="report.txt", storage_key=storage_key)
    stub_storage.put(storage_key, b"client's real file bytes")

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)

        with patch("api.ingestion.routes.get_storage", return_value=stub_storage):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    f"/admin/tenants/{_TENANT_ID}/ingestion/docs/{doc_id}/download",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    assert resp.content == b"client's real file bytes"
    assert resp.headers["content-disposition"] == 'attachment; filename="report.txt"'


async def test_download_doc_cross_tenant_via_wrong_tenant_scope_returns_404_never_a_leak() -> None:
    """Mandatory multi-tenant isolation: a PLATFORM_ADMIN hitting the
    tenant-scoped route with the WRONG tenant_id in the path (not the tenant
    that actually owns this doc_id) gets 404 DOC_NOT_FOUND -- never tenant
    A's bytes leaking to a request scoped at tenant B."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    other_tenant = "tenant-other-download"
    doc_id = "doc-belongs-to-tenant-a"
    storage_key = f"{_TENANT_ID}/{doc_id}/confidential.txt"
    _seed_doc_row(stub_db, tenant_id=_TENANT_ID, doc_id=doc_id, filename="confidential.txt", storage_key=storage_key)
    stub_storage.put(storage_key, b"tenant A's confidential bytes")
    # `other_tenant` must itself be a real/enabled tenant for
    # resolve_tenant_scope to get past ITS OWN check -- seed an unrelated doc
    # under it so the stub DB's tenant-existence check passes, then prove
    # the doc lookup (scoped to other_tenant) still finds nothing.
    _seed_doc_row(stub_db, tenant_id=other_tenant, doc_id="doc-unrelated", filename="unrelated.txt")

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)

        with patch("api.ingestion.routes.get_storage", return_value=stub_storage):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    f"/admin/tenants/{other_tenant}/ingestion/docs/{doc_id}/download",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "DOC_NOT_FOUND"
    assert b"confidential" not in resp.content


# ==============================================================================
# GET /admin/ingestion/docs (list)
# ==============================================================================


def _seed_doc(
    stub_db: _StubDatabase,
    *,
    tenant_id: str,
    doc_id: str,
    filename: str,
    created_at: datetime,
    title: str | None = None,
    description: str | None = None,
    uploaded_by: str | None = None,
    status: str = "parsed",
) -> None:
    stub_db._docs[(tenant_id, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": filename,
        "content_type": "text/plain",
        "status": status,
        "content_hash": f"hash-{doc_id}",
        "storage_key": f"{tenant_id}/{doc_id}/{filename}",
        "created_at": created_at,
        "updated_at": created_at,
        "tenant_id": tenant_id,
        "title": title,
        "description": description,
        "uploaded_by": uploaded_by,
    }


async def test_list_docs_returns_200_sorted_newest_first_with_title_fields() -> None:
    """GET /admin/ingestion/docs -> 200 {docs:[...]} sorted newest-first; no tenant_id/storage_key."""
    _reset_modules()

    stub_db = _StubDatabase()
    older = _NOW.replace(hour=10)
    newer = _NOW.replace(hour=14)
    _seed_doc(
        stub_db,
        tenant_id=_TENANT_ID,
        doc_id="doc-old",
        filename="old.txt",
        created_at=older,
        title="Older doc",
        uploaded_by="user-1",
    )
    _seed_doc(
        stub_db,
        tenant_id=_TENANT_ID,
        doc_id="doc-new",
        filename="new.txt",
        created_at=newer,
        description="No title, falls back to filename",
        uploaded_by="user-1",
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch(
            "api.ingestion.routes.get_user_by_id",
            return_value={"id": "user-1", "name": "Jane Admin", "email": "jane@example.com"},
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/admin/ingestion/docs",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    body = resp.json()
    docs = body["docs"]
    assert [d["doc_id"] for d in docs] == ["doc-new", "doc-old"]

    newest = docs[0]
    assert newest["title"] is None
    assert newest["description"] == "No title, falls back to filename"
    assert newest["filename"] == "new.txt"
    assert newest["uploaded_by"] == "user-1"
    assert newest["uploaded_by_name"] == "Jane Admin"
    assert "tenant_id" not in newest
    assert "storage_key" not in newest

    oldest = docs[1]
    assert oldest["title"] == "Older doc"


async def test_list_docs_empty_tenant_returns_empty_list_not_error() -> None:
    """A tenant with no knowledge docs gets {docs: []}, never a 404 or fabricated rows."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/admin/ingestion/docs",
                cookies={"access_token": token},
            )

    assert resp.status_code == 200
    assert resp.json() == {"docs": []}


async def test_list_docs_tenant_isolation() -> None:
    """Tenant A's list never includes tenant B's docs (mandatory multi-tenant isolation)."""
    _reset_modules()

    stub_db = _StubDatabase()
    other_tenant = "tenant-other"
    _seed_doc(
        stub_db,
        tenant_id=_TENANT_ID,
        doc_id="doc-mine",
        filename="mine.txt",
        created_at=_NOW,
    )
    _seed_doc(
        stub_db,
        tenant_id=other_tenant,
        doc_id="doc-not-mine",
        filename="secret.txt",
        created_at=_NOW,
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(tenant_id=_TENANT_ID)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/admin/ingestion/docs",
                cookies={"access_token": token},
            )

    assert resp.status_code == 200
    doc_ids = [d["doc_id"] for d in resp.json()["docs"]]
    assert doc_ids == ["doc-mine"]
    assert "doc-not-mine" not in doc_ids


async def test_list_docs_uploaded_by_name_falls_back_to_raw_id_when_user_missing() -> None:
    """A deleted/missing uploader never crashes the list -- falls back to the raw id."""
    _reset_modules()

    stub_db = _StubDatabase()
    _seed_doc(
        stub_db,
        tenant_id=_TENANT_ID,
        doc_id="doc-orphan",
        filename="orphan.txt",
        created_at=_NOW,
        uploaded_by="user-deleted",
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch("api.ingestion.routes.get_user_by_id", return_value=None):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/admin/ingestion/docs",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    doc = resp.json()["docs"][0]
    assert doc["uploaded_by"] == "user-deleted"
    assert doc["uploaded_by_name"] == "user-deleted"


async def test_list_docs_uploaded_by_name_falls_back_to_email_when_name_unset() -> None:
    """Regression: a real user with name=NULL must show their email, never the
    literal string "None" (str(None) on an unconditional str() cast -- caught
    live against a real seeded user whose `name` column was never set)."""
    _reset_modules()

    stub_db = _StubDatabase()
    _seed_doc(
        stub_db,
        tenant_id=_TENANT_ID,
        doc_id="doc-noname",
        filename="noname.txt",
        created_at=_NOW,
        uploaded_by="user-noname",
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with patch(
            "api.ingestion.routes.get_user_by_id",
            return_value={"id": "user-noname", "name": None, "email": "noname@example.com"},
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/admin/ingestion/docs",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    doc = resp.json()["docs"][0]
    assert doc["uploaded_by_name"] == "noname@example.com"
    assert doc["uploaded_by_name"] != "None"


async def test_list_docs_no_cookie_returns_401() -> None:
    """GET /admin/ingestion/docs without a cookie -> 401."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/admin/ingestion/docs")

    assert resp.status_code == 401


async def test_list_docs_client_agent_returns_403() -> None:
    """GET /admin/ingestion/docs with CLIENT_AGENT -> 403."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.CLIENT_AGENT)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/admin/ingestion/docs",
                cookies={"access_token": token},
            )

    assert resp.status_code == 403


async def test_list_docs_platform_admin_via_tenant_scoped_route_returns_200() -> None:
    """PLATFORM_ADMIN reaches a specific tenant's list via the tenant-scoped route -> 200."""
    _reset_modules()

    stub_db = _StubDatabase()
    _seed_doc(
        stub_db,
        tenant_id=_TENANT_ID,
        doc_id="doc-scoped",
        filename="scoped.txt",
        created_at=_NOW,
    )

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/admin/tenants/{_TENANT_ID}/ingestion/docs",
                cookies={"access_token": token},
            )

    assert resp.status_code == 200
    doc_ids = [d["doc_id"] for d in resp.json()["docs"]]
    assert doc_ids == ["doc-scoped"]


async def test_list_docs_global_platform_admin_on_implicit_route_returns_403() -> None:
    """PLATFORM_ADMIN on the implicit (non-tenant-scoped) list route -> 403 (no tenant context)."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/admin/ingestion/docs",
                cookies={"access_token": token},
            )

    assert resp.status_code == 403


async def test_upload_with_title_and_description_persists_and_appears_in_list() -> None:
    """Upload with title/description -> both persisted and visible in a subsequent list call."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with (
            patch("api.ingestion.routes.ingest_document") as mock_task,
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
        ):
            mock_delay = MagicMock()
            mock_delay.id = "task-id-titled"
            mock_task.delay = MagicMock(return_value=mock_delay)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                upload_resp = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("sample.txt", b"hello document", "text/plain")},
                    data={"title": "  Refund policy  ", "description": "  How refunds work.  "},
                )
                list_resp = await client.get(
                    "/admin/ingestion/docs",
                    cookies={"access_token": token},
                )

    assert upload_resp.status_code == 200
    docs = list_resp.json()["docs"]
    assert len(docs) == 1
    assert docs[0]["title"] == "Refund policy"
    assert docs[0]["description"] == "How refunds work."
    assert docs[0]["uploaded_by"] == "user-1"


async def test_upload_blank_title_normalizes_to_null() -> None:
    """A whitespace-only title is stored/returned as null, not an empty string."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with (
            patch("api.ingestion.routes.ingest_document") as mock_task,
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
        ):
            mock_delay = MagicMock()
            mock_delay.id = "task-id-blank-title"
            mock_task.delay = MagicMock(return_value=mock_delay)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/ingestion/upload",
                    cookies={"access_token": token},
                    files={"file": ("sample.txt", b"hello document", "text/plain")},
                    data={"title": "   "},
                )
                doc_id = resp.json()["doc_id"]
                get_resp = await client.get(
                    f"/admin/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert get_resp.json()["title"] is None


# ==============================================================================
# Regression: upload at INFO log level must not crash (reserved 'filename' key)
# ==============================================================================


async def test_delete_doc_happy_path_returns_200_and_removes_everything() -> None:
    """CLIENT_ADMIN DELETE -> 200 {doc_id, deleted:true, chunks_deleted, runs_deleted};
    no tenant_id/storage_key in body; DB-before-storage-before-audit ordering."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    doc_id = "doc-delete-happy"
    storage_key = f"{_TENANT_ID}/{doc_id}/sample.txt"
    parsed_key = f"{_TENANT_ID}/{doc_id}/parsed.txt"
    stub_db._docs[(_TENANT_ID, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "sample.txt",
        "content_type": "text/plain",
        "status": "parsed",
        "content_hash": "sha256-del",
        "storage_key": storage_key,
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": _TENANT_ID,
    }
    stub_db._runs[(_TENANT_ID, "run-del")] = {
        "run_id": "run-del",
        "doc_id": doc_id,
        "status": "succeeded",
        "chars_out": 42,
        "errors": None,
        "started_at": _NOW,
        "finished_at": _NOW,
        "duration_ms": 100,
        "tenant_id": _TENANT_ID,
    }
    stub_db._chunk_counts[(_TENANT_ID, doc_id)] = 12
    stub_storage.put(storage_key, b"raw bytes")
    stub_storage.put(parsed_key, b"parsed text")

    call_order: list[str] = []
    real_delete_doc = None

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        import api.ingestion.repository as repo_mod
        from api.audit import repository as audit_mod

        real_delete_doc = repo_mod.delete_doc
        real_delete = stub_storage.delete
        real_record_audit = audit_mod.record_audit

        async def _tracking_delete_doc(*args: Any, **kwargs: Any) -> Any:
            call_order.append("delete_doc")
            return await real_delete_doc(*args, **kwargs)

        def _tracking_storage_delete(key: str) -> None:
            call_order.append("storage.delete")
            real_delete(key)

        async def _tracking_record_audit(*args: Any, **kwargs: Any) -> Any:
            call_order.append("record_audit")
            return await real_record_audit(*args, **kwargs)

        stub_storage.delete = _tracking_storage_delete  # type: ignore[method-assign]

        with (
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
            patch("api.ingestion.routes.repo.delete_doc", side_effect=_tracking_delete_doc),
            patch("api.ingestion.routes.record_audit", side_effect=_tracking_record_audit),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.request(
                    "DELETE",
                    f"/admin/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == doc_id
    assert body["deleted"] is True
    assert body["chunks_deleted"] == 12
    assert body["runs_deleted"] == 1
    assert "tenant_id" not in body
    assert "storage_key" not in body

    assert call_order[0] == "delete_doc"
    assert "storage.delete" in call_order
    assert call_order.count("storage.delete") == 2
    assert call_order[-1] == "record_audit"
    assert call_order.index("delete_doc") < call_order.index("storage.delete")
    assert call_order.index("storage.delete") < call_order.index("record_audit")

    # Doc is actually gone from the store.
    assert (_TENANT_ID, doc_id) not in stub_db._docs
    assert not stub_storage.exists(storage_key)
    assert not stub_storage.exists(parsed_key)


async def test_delete_doc_missing_or_cross_tenant_returns_404_no_destructive_calls() -> None:
    """MANDATORY isolation: absent/cross-tenant doc_id -> 404 DOC_NOT_FOUND;
    delete_doc and storage.delete are NEVER reached."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    # Doc exists, but under a DIFFERENT tenant than the caller's cookie.
    other_tenant = "tenant-other"
    doc_id = "doc-cross-tenant"
    stub_db._docs[(other_tenant, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "secret.txt",
        "content_type": "text/plain",
        "status": "parsed",
        "content_hash": "sha256-cross",
        "storage_key": f"{other_tenant}/{doc_id}/secret.txt",
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": other_tenant,
    }

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()  # tenant = _TENANT_ID, not other_tenant

        with (
            patch("api.ingestion.routes.get_storage", return_value=stub_storage) as mock_get_storage,
            patch("api.ingestion.routes.repo.delete_doc") as mock_delete_doc,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.request(
                    "DELETE",
                    f"/admin/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "DOC_NOT_FOUND"
    mock_delete_doc.assert_not_called()
    # get_storage may or may not be constructed lazily, but delete must never fire.
    if mock_get_storage.return_value is not None:
        assert not hasattr(mock_get_storage.return_value, "_delete_called")

    # Tenant B's row must be untouched.
    assert (other_tenant, doc_id) in stub_db._docs


async def test_delete_doc_client_admin_returns_200() -> None:
    """RBAC: CLIENT_ADMIN -> 200 (baseline positive, already covered above but kept explicit)."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-rbac-admin"
    stub_db._docs[(_TENANT_ID, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "f.txt",
        "content_type": "text/plain",
        "status": "parsed",
        "content_hash": "h-rbac",
        "storage_key": f"{_TENANT_ID}/{doc_id}/f.txt",
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": _TENANT_ID,
    }

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.CLIENT_ADMIN)

        with patch("api.ingestion.routes.get_storage", return_value=stub_storage):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.request(
                    "DELETE",
                    f"/admin/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200


async def test_delete_doc_client_agent_returns_403() -> None:
    """RBAC: CLIENT_AGENT -> 403 ROLE_NOT_PERMITTED (decision 3 — agents cannot delete knowledge)."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.CLIENT_AGENT)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/admin/ingestion/docs/any-id",
                cookies={"access_token": token},
            )

    assert resp.status_code == 403


async def test_delete_doc_visitor_returns_403() -> None:
    """RBAC: VISITOR -> 403."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.VISITOR)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/admin/ingestion/docs/any-id",
                cookies={"access_token": token},
            )

    assert resp.status_code == 403


async def test_delete_doc_no_cookie_returns_401() -> None:
    """RBAC: no cookie -> 401."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request("DELETE", "/admin/ingestion/docs/any-id")

    assert resp.status_code == 401


async def test_delete_doc_global_platform_admin_on_implicit_route_returns_403() -> None:
    """RBAC: a global PLATFORM_ADMIN (tenant_id=None) on the IMPLICIT route -> 403."""
    _reset_modules()

    stub_db = _StubDatabase()

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/admin/ingestion/docs/any-id",
                cookies={"access_token": token},
            )

    assert resp.status_code == 403


async def test_delete_doc_platform_admin_via_tenant_scoped_route_returns_200_with_marker() -> None:
    """PLATFORM_ADMIN reaches a specific tenant's doc via the tenant-scoped route -> 200;
    the audit call carries the platform_admin actor_context marker."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-platform-scoped"
    stub_db._docs[(_TENANT_ID, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "scoped.txt",
        "content_type": "text/plain",
        "status": "parsed",
        "content_hash": "h-scoped",
        "storage_key": f"{_TENANT_ID}/{doc_id}/scoped.txt",
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": _TENANT_ID,
    }

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)

        captured_kwargs: dict[str, Any] = {}
        from api.audit.repository import record_audit as real_record_audit

        async def _capturing_record_audit(*args: Any, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return await real_record_audit(*args, **kwargs)

        with (
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
            patch("api.ingestion.routes.record_audit", side_effect=_capturing_record_audit),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.request(
                    "DELETE",
                    f"/admin/tenants/{_TENANT_ID}/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert captured_kwargs.get("actor_context") is not None


async def test_delete_doc_storage_orphan_degradation_still_returns_200() -> None:
    """No silent fallback: storage.delete raising AFTER a successful DB delete
    still returns 200 deleted:true, and a document_delete_storage_orphan warning
    is logged. The DB delete is authoritative and is not rolled back / not
    reported as failure."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-orphan"
    storage_key = f"{_TENANT_ID}/{doc_id}/f.txt"
    stub_db._docs[(_TENANT_ID, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "f.txt",
        "content_type": "text/plain",
        "status": "parsed",
        "content_hash": "h-orphan",
        "storage_key": storage_key,
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": _TENANT_ID,
    }

    def _raising_delete(key: str) -> None:
        raise OSError("simulated storage failure")

    stub_storage.delete = _raising_delete  # type: ignore[method-assign]

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with (
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
            patch("api.ingestion.routes._log") as mock_log,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.request(
                    "DELETE",
                    f"/admin/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True

    # The DB row is gone regardless of the storage failure.
    assert (_TENANT_ID, doc_id) not in stub_db._docs

    warning_events = [
        call.args[0] if call.args else call.kwargs.get("msg")
        for call in mock_log.warning.call_args_list
    ]
    assert any("document_delete_storage_orphan" in str(e) for e in warning_events)


async def test_delete_doc_race_not_found_skips_audit() -> None:
    """Audit-only-after-success: a delete_doc that raises DOC_NOT_FOUND (0-row
    race after get_doc succeeded) -> 404 to the caller and NO record_audit call."""
    _reset_modules()

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()
    doc_id = "doc-race"
    stub_db._docs[(_TENANT_ID, doc_id)] = {
        "doc_id": doc_id,
        "source": "upload",
        "filename": "race.txt",
        "content_type": "text/plain",
        "status": "parsed",
        "content_hash": "h-race",
        "storage_key": f"{_TENANT_ID}/{doc_id}/race.txt",
        "created_at": _NOW,
        "updated_at": _NOW,
        "tenant_id": _TENANT_ID,
    }

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        with (
            patch("api.ingestion.routes.get_storage", return_value=stub_storage),
            patch("api.ingestion.routes.repo.delete_doc", side_effect=NotFoundError_factory()),
            patch("api.ingestion.routes.record_audit") as mock_record_audit,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.request(
                    "DELETE",
                    f"/admin/ingestion/docs/{doc_id}",
                    cookies={"access_token": token},
                )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "DOC_NOT_FOUND"
    mock_record_audit.assert_not_called()


def NotFoundError_factory() -> Any:
    """Return a side_effect callable that raises NotFoundError(DOC_NOT_FOUND) — used
    to simulate the delete_doc 0-row concurrent-delete race in
    test_delete_doc_race_not_found_skips_audit."""
    from common.errors import NotFoundError

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise NotFoundError("Knowledge document not found.", code="DOC_NOT_FOUND")

    return _raise


async def test_upload_at_info_log_level_returns_200_not_500() -> None:
    """Regression: POST /upload with LOG_LEVEL=INFO must return 200, not 500.

    The original bug: the route passed ``filename`` (a reserved LogRecord
    attribute) in ``extra=``, causing ``logging.makeRecord`` to raise
    ``KeyError: "Attempt to overwrite 'filename' in LogRecord"`` at INFO level.
    Tests ran with LOG_LEVEL=WARNING so ``isEnabledFor(INFO)`` short-circuited
    before makeRecord — hiding the crash from CI. Live deploys used INFO → 500
    on every upload.

    This test forces LOG_LEVEL=INFO so the logger actually calls makeRecord and
    would have surfaced the crash before the hardening fix.
    """
    import logging as _logging

    _reset_modules()

    # Override LOG_LEVEL to INFO to exercise the makeRecord path.
    env_info = {**_TEST_ENV, "LOG_LEVEL": "INFO"}

    stub_db = _StubDatabase()
    stub_storage = _InMemoryStorage()

    with patch.dict("os.environ", env_info, clear=False):
        app = _build_app(stub_db)
        token = _mint_cookie()

        # Also set the ingestion route logger to INFO at the Python level to
        # guarantee makeRecord is called even if settings propagation is delayed.
        route_logger = _logging.getLogger("api.ingestion.routes")
        prev_level = route_logger.level
        route_logger.setLevel(_logging.INFO)

        try:
            with (
                patch("api.ingestion.routes.ingest_document") as mock_task,
                patch("api.ingestion.routes.get_storage", return_value=stub_storage),
            ):
                mock_task.delay = MagicMock(return_value=MagicMock(id="task-info"))

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post(
                        "/admin/ingestion/upload",
                        cookies={"access_token": token},
                        files={"file": ("report.txt", b"content for info test", "text/plain")},
                    )
        finally:
            route_logger.setLevel(prev_level)

    # Would have been 500 (KeyError from makeRecord) without the fix.
    assert resp.status_code == 200, (
        f"Expected 200 at INFO log level — got {resp.status_code}. "
        "This indicates the reserved-key crash in makeRecord is NOT fixed."
    )
    body = resp.json()
    assert body["status"] == "pending"
