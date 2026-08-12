"""Object-storage abstraction for the document-ingestion pipeline.

``StorageProvider`` is a ``typing.Protocol`` defining the four operations every
driver must implement. ``LocalStorageProvider`` is the first driver — it roots
files under ``settings.storage_local_root`` and enforces **tenant-scoped,
traversal-proof** key paths.

Key contract:
- Keys MUST start with a tenant prefix: ``{tenant_id}/{doc_id}/...``
- Absolute keys (starting with ``/``) are rejected.
- Path-traversal components (``..``) in any segment are rejected.
- Both checks raise ``ValidationError`` (code ``INVALID_STORAGE_KEY``).

A key violation at the driver level means tenant A's key can never resolve
inside tenant B's directory, even if a caller forgets the prefix check.

Selecting a driver:
  ``get_storage()`` reads ``settings.storage_backend`` and returns the
  appropriate ``StorageProvider`` instance: ``"local"`` (filesystem) or
  ``"s3"`` (any S3-wire-compatible object store -- AWS S3, Cloudflare R2,
  DigitalOcean Spaces, MinIO, Backblaze B2 -- via ``endpoint_url``). GCS
  slots in later via another ``elif``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from common.errors import ValidationError

from api.config import get_api_settings


@runtime_checkable
class StorageProvider(Protocol):
    """Protocol every storage driver must satisfy."""

    def put(self, key: str, data: bytes) -> None:
        """Write ``data`` under ``key``, creating intermediate directories."""
        ...

    def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key``.

        Raises ``FileNotFoundError`` if the key does not exist.
        """
        ...

    def exists(self, key: str) -> bool:
        """Return ``True`` if ``key`` is present in storage."""
        ...

    def delete(self, key: str) -> None:
        """Delete the object at ``key``.

        No-op if the key does not exist.
        """
        ...


def _validate_key(key: str) -> None:
    """Reject absolute paths and path-traversal components.

    Raises ``ValidationError`` (``INVALID_STORAGE_KEY``) on violation.
    All callers pass tenant-prefixed keys, so traversal out of the tenant
    directory is structurally impossible once these two checks pass.
    """
    if key.startswith("/"):
        raise ValidationError(
            "Storage key must not be an absolute path.",
            code="INVALID_STORAGE_KEY",
        )
    # Normalise to forward slashes before checking segments.
    parts = key.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValidationError(
            "Storage key must not contain path-traversal components (..).",
            code="INVALID_STORAGE_KEY",
        )


class LocalStorageProvider:
    """File-system storage driver rooted at ``root``.

    Constructed by ``get_storage()`` which passes ``settings.storage_local_root``.
    Fail-fast: raises ``RuntimeError`` if ``root`` is ``None`` or empty — the
    caller must have set ``STORAGE_LOCAL_ROOT`` (CLAUDE.md §3 config).
    """

    def __init__(self, root: str | None) -> None:
        if not root:
            raise RuntimeError(
                "LocalStorageProvider requires STORAGE_LOCAL_ROOT to be set. "
                "See deploy/.env.example for documentation."
            )
        self._root = Path(root).resolve()

    def _full_path(self, key: str) -> Path:
        _validate_key(key)
        return self._root / key

    def put(self, key: str, data: bytes) -> None:
        """Write ``data`` under ``key``, creating parent directories."""
        path = self._full_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        """Return bytes at ``key``; raises ``FileNotFoundError`` if absent."""
        return self._full_path(key).read_bytes()

    def exists(self, key: str) -> bool:
        """Return ``True`` if ``key`` exists on disk."""
        return self._full_path(key).exists()

    def delete(self, key: str) -> None:
        """Delete file at ``key``; silent no-op if absent."""
        path = self._full_path(key)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# Botocore's not-found error codes across the calls this driver makes
# (HeadObject returns "404"; GetObject returns "NoSuchKey" -- both are
# "the object isn't there", never a real failure worth surfacing as one).
_S3_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey"})


class S3StorageProvider:
    """S3-wire-compatible object storage driver.

    Works against real AWS S3 or any S3-compatible store (Cloudflare R2,
    DigitalOcean Spaces, MinIO, Backblaze B2, ...) by pointing ``endpoint_url``
    at that provider -- this is what makes the multi-service deploy topology
    (a separate API process and Celery worker process, each with its own
    ephemeral filesystem) work: both processes talk to the same bucket
    instead of each other's local disk.

    Constructed by ``get_storage()`` which passes ``settings.storage_s3_*``.
    Fail-fast: raises ``RuntimeError`` if ``bucket`` is ``None``/empty -- the
    caller must have set ``STORAGE_S3_BUCKET`` (CLAUDE.md §3 config).

    ``client`` accepts a pre-built boto3 S3 client (or any object shaped like
    one) so callers/tests can inject a fake without a real network call or
    the ``moto`` dependency -- mirrors the ``client: Any | None`` injection
    already used by ``api.llm.openai_provider.OpenAICompatibleProvider``.
    Credentials are never passed as literals here: when ``client`` is not
    injected, boto3 resolves them from ``access_key_id``/``secret_access_key``
    if given, otherwise its own default chain (env vars, IAM role, etc.).
    """

    def __init__(
        self,
        *,
        bucket: str | None,
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise RuntimeError(
                "S3StorageProvider requires STORAGE_S3_BUCKET to be set. "
                "See deploy/.env.example for documentation."
            )
        self._bucket = bucket
        if client is not None:
            self._client = client
        else:
            import boto3  # noqa: PLC0415

            self._client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
            )

    def put(self, key: str, data: bytes) -> None:
        """Write ``data`` under ``key``."""
        _validate_key(key)
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        """Return bytes at ``key``; raises ``FileNotFoundError`` if absent."""
        _validate_key(key)
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _S3_NOT_FOUND_CODES:
                raise FileNotFoundError(key) from exc
            raise
        result: bytes = resp["Body"].read()
        return result

    def exists(self, key: str) -> bool:
        """Return ``True`` if ``key`` exists in the bucket."""
        _validate_key(key)
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _S3_NOT_FOUND_CODES:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        """Delete object at ``key``; no-op if absent (S3 semantics)."""
        _validate_key(key)
        self._client.delete_object(Bucket=self._bucket, Key=key)


def get_storage() -> StorageProvider:
    """Return the configured ``StorageProvider`` instance.

    Resolution order:
    1. Read ``settings.storage_backend`` (default ``"local"``).
    2. Construct and return the matching driver.
    3. Unknown backend → ``ValidationError`` (``UNSUPPORTED_STORAGE_BACKEND``).

    Fail-fast: ``LocalStorageProvider`` raises ``RuntimeError`` if
    ``storage_local_root`` is unset; ``S3StorageProvider`` raises
    ``RuntimeError`` if ``storage_s3_bucket`` is unset (CLAUDE.md §3 config).
    """
    settings = get_api_settings()
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalStorageProvider(settings.storage_local_root)
    if backend == "s3":
        return S3StorageProvider(
            bucket=settings.storage_s3_bucket,
            region=settings.storage_s3_region,
            endpoint_url=settings.storage_s3_endpoint_url,
            access_key_id=settings.storage_s3_access_key_id,
            secret_access_key=settings.storage_s3_secret_access_key,
        )
    raise ValidationError(
        f"Unsupported storage backend: {backend!r}. Supported: 'local', 's3'.",
        code="UNSUPPORTED_STORAGE_BACKEND",
    )
