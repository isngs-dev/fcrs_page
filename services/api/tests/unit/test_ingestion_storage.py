"""Unit tests for api.ingestion.storage.

Covers:
- LocalStorageProvider put/get/exists/delete round-trip (tmp root).
- Keys are tenant-prefixed; a key under tenant A is not reachable via tenant B prefix.
- Path-traversal (``..``) and absolute keys are rejected (ValidationError).
- Fail-fast when storage_local_root is unset.
- get_storage() returns LocalStorageProvider for backend="local".
- get_storage() raises for unknown backend.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

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
}


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


# ==============================================================================
# LocalStorageProvider round-trips
# ==============================================================================


def test_local_put_get_round_trip(tmp_path: object) -> None:
    """put then get returns the same bytes."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        key = "tenant-a/doc-1/sample.txt"
        data = b"hello world"
        sp.put(key, data)
        assert sp.get(key) == data


def test_local_exists_true_after_put(tmp_path: object) -> None:
    """exists returns True after put."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        key = "tenant-a/doc-2/file.txt"
        assert not sp.exists(key)
        sp.put(key, b"content")
        assert sp.exists(key)


def test_local_delete_removes_file(tmp_path: object) -> None:
    """delete removes the file; exists returns False afterwards."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        key = "tenant-a/doc-3/file.txt"
        sp.put(key, b"to delete")
        assert sp.exists(key)
        sp.delete(key)
        assert not sp.exists(key)


def test_local_delete_missing_key_is_noop(tmp_path: object) -> None:
    """delete of a non-existent key does not raise."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        sp.delete("tenant-a/doc-99/nope.txt")  # should not raise


def test_local_get_missing_raises_file_not_found(tmp_path: object) -> None:
    """get of an absent key raises FileNotFoundError."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            sp.get("tenant-a/doc-5/missing.txt")


# ==============================================================================
# Tenant isolation: key under tenant A unreachable via tenant B prefix
# ==============================================================================


def test_local_tenant_isolation(tmp_path: object) -> None:
    """A file stored under tenant-a/ is not found via a tenant-b/ prefix."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        sp.put("tenant-a/doc-1/sample.txt", b"secret data")
        assert not sp.exists("tenant-b/doc-1/sample.txt")
        with pytest.raises(FileNotFoundError):
            sp.get("tenant-b/doc-1/sample.txt")


# ==============================================================================
# Security: path-traversal and absolute keys rejected
# ==============================================================================


def test_local_rejects_absolute_key(tmp_path: object) -> None:
    """An absolute key (starting with /) raises ValidationError."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        with pytest.raises(ValidationError) as exc_info:
            sp.put("/etc/passwd", b"nope")
        assert exc_info.value.code == "INVALID_STORAGE_KEY"


def test_local_rejects_dotdot_traversal(tmp_path: object) -> None:
    """A key containing '..' raises ValidationError."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        with pytest.raises(ValidationError) as exc_info:
            sp.put("tenant-a/../tenant-b/secret.txt", b"nope")
        assert exc_info.value.code == "INVALID_STORAGE_KEY"


def test_local_rejects_dotdot_in_exists(tmp_path: object) -> None:
    """exists() also rejects path-traversal keys."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.storage import LocalStorageProvider

        sp = LocalStorageProvider(str(tmp_path))
        with pytest.raises(ValidationError) as exc_info:
            sp.exists("../../etc/passwd")
        assert exc_info.value.code == "INVALID_STORAGE_KEY"


# ==============================================================================
# Fail-fast when storage_local_root is unset
# ==============================================================================


def test_local_raises_when_root_is_none() -> None:
    """LocalStorageProvider raises RuntimeError when root is None."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        with pytest.raises(RuntimeError, match="STORAGE_LOCAL_ROOT"):
            LocalStorageProvider(None)


def test_local_raises_when_root_is_empty_string() -> None:
    """LocalStorageProvider raises RuntimeError when root is an empty string."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import LocalStorageProvider

        with pytest.raises(RuntimeError, match="STORAGE_LOCAL_ROOT"):
            LocalStorageProvider("")


# ==============================================================================
# get_storage() selector
# ==============================================================================


def test_get_storage_returns_local_provider(tmp_path: object) -> None:
    """get_storage() with backend=local returns a LocalStorageProvider."""
    _reset_modules()
    env = {**_TEST_ENV, "STORAGE_BACKEND": "local", "STORAGE_LOCAL_ROOT": str(tmp_path)}
    with patch.dict("os.environ", env, clear=False):
        from api.config import get_api_settings

        get_api_settings.cache_clear()
        from api.ingestion.storage import LocalStorageProvider, get_storage

        provider = get_storage()
        assert isinstance(provider, LocalStorageProvider)


def test_get_storage_raises_for_unknown_backend() -> None:
    """get_storage() with an unknown backend raises ValidationError."""
    _reset_modules()
    env = {**_TEST_ENV, "STORAGE_BACKEND": "gcs"}
    with patch.dict("os.environ", env, clear=False):
        from api.config import get_api_settings

        get_api_settings.cache_clear()
        from common.errors import ValidationError

        from api.ingestion.storage import get_storage

        with pytest.raises(ValidationError) as exc_info:
            get_storage()
        assert exc_info.value.code == "UNSUPPORTED_STORAGE_BACKEND"


# ==============================================================================
# S3StorageProvider — put/get/exists/delete round-trip against an injected
# fake client (no network, no moto: mirrors the OpenAICompatibleProvider
# ``client: Any | None`` injection pattern already used in api.llm).
# ==============================================================================


def _fake_s3_client() -> object:
    """A minimal in-memory stand-in for a boto3 S3 client.

    Implements exactly the operations S3StorageProvider calls
    (put_object/get_object/head_object/delete_object) plus the botocore
    ``ClientError`` shape those calls raise on a missing key, so
    S3StorageProvider's own error-translation logic is exercised for real.
    """
    from botocore.exceptions import ClientError

    class _FakeS3Client:
        def __init__(self) -> None:
            self._objects: dict[str, bytes] = {}

        def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
            self._objects[Key] = Body

        def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            if Key not in self._objects:
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                    "GetObject",
                )
            body = self._objects[Key]

            class _Body:
                def read(self_inner) -> bytes:  # noqa: N805
                    return body

            return {"Body": _Body()}

        def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            if Key not in self._objects:
                raise ClientError(
                    {"Error": {"Code": "404", "Message": "Not found"}},
                    "HeadObject",
                )
            return {}

        def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
            self._objects.pop(Key, None)

    return _FakeS3Client()


def test_s3_put_get_round_trip() -> None:
    """put then get returns the same bytes."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        key = "tenant-a/doc-1/sample.txt"
        data = b"hello world"
        sp.put(key, data)
        assert sp.get(key) == data


def test_s3_exists_true_after_put() -> None:
    """exists returns True after put, False before."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        key = "tenant-a/doc-2/file.txt"
        assert not sp.exists(key)
        sp.put(key, b"content")
        assert sp.exists(key)


def test_s3_delete_removes_object() -> None:
    """delete removes the object; exists returns False afterwards."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        key = "tenant-a/doc-3/file.txt"
        sp.put(key, b"to delete")
        assert sp.exists(key)
        sp.delete(key)
        assert not sp.exists(key)


def test_s3_delete_missing_key_is_noop() -> None:
    """delete of a non-existent key does not raise."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        sp.delete("tenant-a/doc-99/nope.txt")  # should not raise


def test_s3_get_missing_raises_file_not_found() -> None:
    """get of an absent key raises FileNotFoundError (translated from ClientError)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        with pytest.raises(FileNotFoundError):
            sp.get("tenant-a/doc-5/missing.txt")


def test_s3_tenant_isolation() -> None:
    """A key stored under tenant-a/ is not found via a tenant-b/ prefix."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        sp.put("tenant-a/doc-1/sample.txt", b"secret data")
        assert not sp.exists("tenant-b/doc-1/sample.txt")
        with pytest.raises(FileNotFoundError):
            sp.get("tenant-b/doc-1/sample.txt")


def test_s3_rejects_absolute_key() -> None:
    """An absolute key (starting with /) raises ValidationError before any client call."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        with pytest.raises(ValidationError) as exc_info:
            sp.put("/etc/passwd", b"nope")
        assert exc_info.value.code == "INVALID_STORAGE_KEY"


def test_s3_rejects_dotdot_traversal() -> None:
    """A key containing '..' raises ValidationError before any client call."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.storage import S3StorageProvider

        sp = S3StorageProvider(bucket="test-bucket", client=_fake_s3_client())
        with pytest.raises(ValidationError) as exc_info:
            sp.put("tenant-a/../tenant-b/secret.txt", b"nope")
        assert exc_info.value.code == "INVALID_STORAGE_KEY"


def test_s3_raises_when_bucket_is_none() -> None:
    """S3StorageProvider raises RuntimeError when bucket is None."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        with pytest.raises(RuntimeError, match="STORAGE_S3_BUCKET"):
            S3StorageProvider(bucket=None, client=_fake_s3_client())


def test_s3_raises_when_bucket_is_empty_string() -> None:
    """S3StorageProvider raises RuntimeError when bucket is an empty string."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.storage import S3StorageProvider

        with pytest.raises(RuntimeError, match="STORAGE_S3_BUCKET"):
            S3StorageProvider(bucket="", client=_fake_s3_client())


def test_s3_other_client_errors_propagate_from_exists() -> None:
    """A ClientError that isn't a not-found code propagates (not swallowed as False)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from botocore.exceptions import ClientError

        from api.ingestion.storage import S3StorageProvider

        client = _fake_s3_client()

        def _boom(**kwargs: object) -> None:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "HeadObject"
            )

        client.head_object = _boom  # type: ignore[attr-defined]
        sp = S3StorageProvider(bucket="test-bucket", client=client)
        with pytest.raises(ClientError):
            sp.exists("tenant-a/doc-1/file.txt")


# ==============================================================================
# get_storage() selector — s3 backend
# ==============================================================================


def test_get_storage_returns_s3_provider() -> None:
    """get_storage() with backend=s3 returns an S3StorageProvider."""
    _reset_modules()
    env = {**_TEST_ENV, "STORAGE_BACKEND": "s3", "STORAGE_S3_BUCKET": "test-bucket"}
    with patch.dict("os.environ", env, clear=False):
        from api.config import get_api_settings

        get_api_settings.cache_clear()
        from api.ingestion.storage import S3StorageProvider, get_storage

        provider = get_storage()
        assert isinstance(provider, S3StorageProvider)


def test_get_storage_s3_fails_fast_without_bucket() -> None:
    """get_storage() with backend=s3 and no bucket set raises RuntimeError."""
    _reset_modules()
    env = {**_TEST_ENV, "STORAGE_BACKEND": "s3"}
    with patch.dict("os.environ", env, clear=False):
        from api.config import get_api_settings

        get_api_settings.cache_clear()
        from api.ingestion.storage import get_storage

        with pytest.raises(RuntimeError, match="STORAGE_S3_BUCKET"):
            get_storage()
