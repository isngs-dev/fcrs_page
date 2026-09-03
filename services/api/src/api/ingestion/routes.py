"""Ingestion endpoints — upload + read.

Both endpoints require ``CLIENT_ADMIN`` (RBAC, CLAUDE.md §3).

POST /admin/ingestion/upload
    Multipart ``file`` field. Validates size and content type, computes
    SHA-256, checks for an existing doc with the same hash (idempotent
    re-upload → return existing doc_id, no second enqueue), stores raw bytes,
    inserts ``knowledge_docs`` + ``ingestion_runs``, enqueues
    ``ingestion.ingest_document``.

GET /admin/ingestion/docs
    (Knowledge Base list feature) Returns every doc for the caller's tenant,
    newest upload first (``ORDER BY created_at DESC``), as
    ``{"docs": [...]}``. Each entry never includes ``tenant_id``,
    ``storage_key``, or ``content_hash``. ``uploaded_by_name`` is a
    best-effort display-name resolution of ``uploaded_by`` via
    ``auth.repository.get_user_by_id`` (deduped per request); falls back to
    the raw id if the user record is gone. An empty tenant returns
    ``{"docs": []}``, never a 404 or fabricated rows.

GET /admin/ingestion/docs/{doc_id}
    Returns doc + latest run + first 500 chars of ``parsed.txt`` (if
    available). Response NEVER includes ``tenant_id`` or ``storage_key``.
    Returns 404 ``DOC_NOT_FOUND`` if absent / not visible to the caller.

GET /admin/ingestion/docs/{doc_id}/download
    Streams the ORIGINAL uploaded file back (raw bytes from
    ``StorageProvider.get(doc.storage_key)``), with ``Content-Type`` set to
    the doc's own ``content_type`` and a sanitized
    ``Content-Disposition: attachment; filename="..."`` header (CR/LF and
    ``"`` stripped from the user-supplied filename first — the one place
    this endpoint puts client-controlled input into a raw HTTP header).
    Same tenant-isolated ``get_doc`` lookup as the GET above — 404
    ``DOC_NOT_FOUND`` if absent/not visible, never a leak. Unlike the GET
    above's best-effort preview, a missing stored file here is a real
    error (500 ``DOCUMENT_STORAGE_MISSING``), not a silent empty download —
    the file IS the point of this endpoint.

DELETE /admin/ingestion/docs/{doc_id}
    (SR-4) Hard-deletes a knowledge document: authorizes via the existing
    tenant-scoped ``get_doc`` (404 before any delete for absent/cross-tenant
    doc_id — no leak), then removes its ``knowledge_chunks``, its
    ``ingestion_runs``, the ``knowledge_docs`` row (DB-first), then the two
    stored files (raw upload + ``parsed.txt``) via the existing
    ``StorageProvider.delete``. A storage-delete failure AFTER a successful
    DB delete is logged as ``document_delete_storage_orphan`` and does not
    fail the request (the DB delete is authoritative). Records a
    ``document_deleted`` audit event after the DB delete succeeds. Response
    NEVER includes ``tenant_id`` or ``storage_key``.
"""
from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims, Role
from common.errors import InternalServerError, NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.auth.repository import get_user_by_id
from api.config import get_api_settings
from api.ingestion import repository as repo
from api.ingestion.storage import get_storage
from api.ingestion.tasks import ingest_document

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/ingestion", tags=["ingestion"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/ingestion", tags=["ingestion"])

_ALLOWED_CONTENT_TYPES = {
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _normalize_optional_text(value: str | None) -> str | None:
    """Blank/whitespace-only -> None; otherwise the trimmed string.

    Applied to admin-supplied ``title``/``description`` so an all-whitespace
    submission is stored as NULL, not an empty-string, keeping the DB/UI
    fallback-to-filename logic simple (checks for ``None``, not `""`).
    """
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


async def _upload_document(
    file: UploadFile,
    request: Request,
    claims: AuthClaims,
    *,
    title: str | None = None,
    description: str | None = None,
) -> Any:
    """Accept a document upload, store it, and enqueue the ingestion task.

    Returns ``{doc_id, run_id, status:"pending"}`` on a fresh upload.
    Returns ``{doc_id, run_id: null, status:"<existing-status>"}`` on an
    idempotent re-upload (same bytes, no new run enqueued) -- ``title``/
    ``description`` are NOT applied to the existing doc on this path (the
    doc already exists under its original metadata).
    Returns 413 JSONResponse when the upload exceeds the configured byte limit.
    """
    from common.logging import _correlation_id  # noqa: PLC2701, PLC0415

    cid = _correlation_id.get() or ""
    settings = get_api_settings()
    db = request.app.state.db

    # -- Validate content type ------------------------------------------------
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"Unsupported content type: {file.content_type!r}. "
            "Supported: text/plain, "
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.",
            code="UNSUPPORTED_CONTENT_TYPE",
        )

    # -- Read bytes + validate size -------------------------------------------
    data = await file.read()
    if len(data) > settings.ingestion_max_upload_bytes:
        return JSONResponse(
            status_code=413,
            content={
                "error_code": "FILE_TOO_LARGE",
                "message": (
                    f"Upload exceeds the {settings.ingestion_max_upload_bytes}-byte limit."
                ),
                "correlation_id": cid,
            },
        )

    # -- Content hash for idempotency -----------------------------------------
    content_hash = hashlib.sha256(data).hexdigest()

    existing = await repo.find_doc_by_hash(db, claims, content_hash)
    if existing is not None:
        # Idempotent re-upload: same content already stored — return the
        # existing doc_id without re-storing, re-inserting, or re-enqueuing.
        _log.info(
            "document_upload_idempotent",
            extra={
                "event": "document_upload_idempotent",
            },
        )
        return {
            "doc_id": existing.doc_id,
            "run_id": None,
            "status": existing.status,
        }

    # -- Store raw bytes -------------------------------------------------------
    # Key pattern: {tenant_id}/{doc_id}/{filename} (decision 3).
    doc_id = uuid4().hex
    filename = file.filename or "upload"
    storage_key = f"{claims.tenant_id}/{doc_id}/{filename}"
    storage = get_storage()
    storage.put(storage_key, data)

    # -- Persist doc + run records --------------------------------------------
    await repo.create_doc(
        db,
        claims,
        source="upload",
        filename=filename,
        content_type=content_type,
        content_hash=content_hash,
        storage_key=storage_key,
        doc_id=doc_id,
        title=_normalize_optional_text(title),
        description=_normalize_optional_text(description),
        uploaded_by=claims.subject,
    )
    run = await repo.create_run(db, claims, doc_id=doc_id)

    # -- Enqueue task ----------------------------------------------------------
    ingest_document.delay(
        doc_id=doc_id,
        tenant_id=claims.tenant_id,
        run_id=run.run_id,
        correlation_id=cid,
    )

    await record_audit(
        db,
        claims,
        action="document_uploaded",
        target_type="knowledge_doc",
        target_id=doc_id,
        metadata={"filename": filename, "content_type": content_type},
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "document_uploaded",
        extra={
            "event": "document_uploaded",
        },
    )

    return {"doc_id": doc_id, "run_id": run.run_id, "status": "pending"}


@router.post("/upload", response_model=None)
async def upload_document(
    file: UploadFile,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
    title: str | None = Form(None),  # noqa: B008
    description: str | None = Form(None),  # noqa: B008
) -> Any:
    return await _upload_document(file, request, claims, title=title, description=description)


@tenant_scoped_router.post("/upload", response_model=None)
async def upload_document_for_tenant(
    file: UploadFile,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
    title: str | None = Form(None),  # noqa: B008
    description: str | None = Form(None),  # noqa: B008
) -> Any:
    """PLATFORM_ADMIN super-user variant of ``POST /admin/ingestion/upload`` (S12.7)."""
    return await _upload_document(file, request, claims, title=title, description=description)


async def _list_documents(request: Request, claims: AuthClaims) -> dict[str, Any]:
    """Return every knowledge doc for the caller's tenant, newest upload first.

    Response NEVER includes ``tenant_id``, ``storage_key``, or
    ``content_hash``. ``uploaded_by_name`` is a best-effort display-name
    resolution of each unique ``uploaded_by`` id via
    ``auth.repository.get_user_by_id`` (deduped -- one lookup per unique
    uploader, not one per doc): prefers the user's ``name``, falls back to
    their ``email`` when ``name`` is unset (nullable column -- a real user
    row with no display name set is a normal case, not missing data), and
    falls back to the raw id when the user record itself is missing (e.g. a
    deleted admin) rather than erroring.
    """
    db = request.app.state.db

    docs = await repo.list_docs(db, claims)

    uploader_ids = {doc.uploaded_by for doc in docs if doc.uploaded_by is not None}
    uploader_names: dict[str, str] = {}
    for uploader_id in uploader_ids:
        user = await get_user_by_id(db, uploader_id)
        if user is None:
            uploader_names[uploader_id] = uploader_id
        else:
            uploader_names[uploader_id] = str(user["name"] or user["email"] or uploader_id)

    return {
        "docs": [
            {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "description": doc.description,
                "filename": doc.filename,
                "content_type": doc.content_type,
                "status": doc.status,
                "uploaded_by": doc.uploaded_by,
                "uploaded_by_name": (
                    uploader_names.get(doc.uploaded_by) if doc.uploaded_by is not None else None
                ),
                "created_at": doc.created_at,
            }
            for doc in docs
        ]
    }


@router.get("/docs")
async def list_documents(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    return await _list_documents(request, claims)


@tenant_scoped_router.get("/docs")
async def list_documents_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/ingestion/docs`` (S12.7)."""
    return await _list_documents(request, claims)


async def _get_document(doc_id: str, request: Request, claims: AuthClaims) -> dict[str, Any]:
    """Return doc metadata + latest run + parsed preview.

    Response NEVER includes ``tenant_id`` or ``storage_key``.
    Returns 404 ``DOC_NOT_FOUND`` if absent or not visible.
    """
    db = request.app.state.db

    doc = await repo.get_doc(db, claims, doc_id)
    if doc is None:
        raise NotFoundError(
            "Knowledge document not found.",
            code="DOC_NOT_FOUND",
        )

    latest_run = await repo.get_latest_run(db, claims, doc_id)

    # Attempt to read the first 500 chars of parsed.txt from storage.
    parsed_preview: str | None = None
    try:
        storage = get_storage()
        parsed_key = f"{claims.tenant_id}/{doc_id}/parsed.txt"
        if storage.exists(parsed_key):
            raw = storage.get(parsed_key)
            parsed_preview = raw.decode("utf-8", errors="replace")[:500]
    except Exception:
        # Storage read failure is non-fatal for the read endpoint — we return
        # the doc record with parsed_preview=null rather than 500-ing.
        parsed_preview = None

    run_payload: dict[str, Any] | None = None
    if latest_run is not None:
        run_payload = {
            "run_id": latest_run.run_id,
            "status": latest_run.status,
            "chars_out": latest_run.chars_out,
            "errors": latest_run.errors,
            "duration_ms": latest_run.duration_ms,
        }

    return {
        "doc_id": doc.doc_id,
        "filename": doc.filename,
        "content_type": doc.content_type,
        "status": doc.status,
        "content_hash": doc.content_hash,
        "latest_run": run_payload,
        "parsed_preview": parsed_preview,
        "title": doc.title,
        "description": doc.description,
        "uploaded_by": doc.uploaded_by,
    }


@router.get("/docs/{doc_id}")
async def get_document(
    doc_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    return await _get_document(doc_id, request, claims)


@tenant_scoped_router.get("/docs/{doc_id}")
async def get_document_for_tenant(
    doc_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/ingestion/docs/{doc_id}`` (S12.7)."""
    return await _get_document(doc_id, request, claims)


def _sanitize_content_disposition_filename(filename: str) -> str:
    """Strip CR/LF and ``"`` from a client-supplied filename before it goes
    into a raw ``Content-Disposition`` header value — closes off header
    injection via a malicious upload filename (the one place this endpoint
    puts user input into a header)."""
    return filename.replace("\r", "").replace("\n", "").replace('"', "")


async def _download_document(doc_id: str, request: Request, claims: AuthClaims) -> Response:
    """Stream the original uploaded file back. See module docstring."""
    db = request.app.state.db

    doc = await repo.get_doc(db, claims, doc_id)
    if doc is None:
        raise NotFoundError(
            "Knowledge document not found.",
            code="DOC_NOT_FOUND",
        )

    storage = get_storage()
    try:
        raw = storage.get(doc.storage_key)
    except Exception as exc:
        # Unlike _get_document's best-effort preview, the file IS the point
        # of a download request — a storage miss is a real error, never a
        # silently empty/fabricated download.
        raise InternalServerError(
            "The stored file for this document is missing.",
            code="DOCUMENT_STORAGE_MISSING",
        ) from exc

    filename = _sanitize_content_disposition_filename(doc.filename)
    return Response(
        content=raw,
        media_type=doc.content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/docs/{doc_id}/download")
async def download_document(
    doc_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> Response:
    return await _download_document(doc_id, request, claims)


@tenant_scoped_router.get("/docs/{doc_id}/download")
async def download_document_for_tenant(
    doc_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> Response:
    """PLATFORM_ADMIN super-user variant of
    ``GET /admin/ingestion/docs/{doc_id}/download`` (S12.7)."""
    return await _download_document(doc_id, request, claims)


async def _delete_document(doc_id: str, request: Request, claims: AuthClaims) -> dict[str, Any]:
    """Hard-delete a knowledge document: DB rows, then stored files (SR-4).

    Authorizes via the existing tenant-scoped ``get_doc`` first — 404
    ``DOC_NOT_FOUND`` for an absent or cross-tenant doc_id, indistinguishable,
    before any delete runs (no leak). Deletes DB rows first (decision 5:
    authoritative, correctness-critical state), then removes the two stored
    files; a storage failure after a successful DB delete is logged and does
    NOT fail the request. Audits ``document_deleted`` only after the DB
    delete succeeds.
    """
    db = request.app.state.db

    doc = await repo.get_doc(db, claims, doc_id)
    if doc is None:
        raise NotFoundError(
            "Knowledge document not found.",
            code="DOC_NOT_FOUND",
        )

    # Capture before delete — the row is gone after.
    filename = doc.filename
    raw_key = doc.storage_key

    chunks_deleted, runs_deleted = await repo.delete_doc(db, claims, doc_id)

    # -- Storage cleanup (after DB success; never blocks the response) --------
    storage = get_storage()
    parsed_key = f"{claims.tenant_id}/{doc_id}/parsed.txt"
    for key in (raw_key, parsed_key):
        try:
            storage.delete(key)
        except Exception:
            _log.warning(
                "document_delete_storage_orphan",
                extra={
                    "event": "document_delete_storage_orphan",
                },
            )

    await record_audit(
        db,
        claims,
        action="document_deleted",
        target_type="knowledge_doc",
        target_id=doc_id,
        metadata={
            "filename": filename,
            "chunks_deleted": chunks_deleted,
            "runs_deleted": runs_deleted,
        },
        actor_context=get_platform_admin_actor(request),
    )

    _log.info(
        "document_deleted",
        extra={
            "event": "document_deleted",
        },
    )

    return {
        "doc_id": doc_id,
        "deleted": True,
        "chunks_deleted": chunks_deleted,
        "runs_deleted": runs_deleted,
    }


@router.delete("/docs/{doc_id}")
async def delete_document(
    doc_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    return await _delete_document(doc_id, request, claims)


@tenant_scoped_router.delete("/docs/{doc_id}")
async def delete_document_for_tenant(
    doc_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, Any]:
    """PLATFORM_ADMIN super-user variant of ``DELETE /admin/ingestion/docs/{doc_id}`` (S12.7)."""
    return await _delete_document(doc_id, request, claims)
