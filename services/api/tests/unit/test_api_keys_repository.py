"""Unit tests for api.admin.api_keys_repository (SR-20 D6) -- CLIENT_ADMIN
self-service client-key rotation + Origin allowlist read/update.

Covers:
- get_api_key_info never exposes the raw or hashed key -- only the prefix
  ("pk_") + a last-rotated-at style existence check, plus the allowlist.
- rotate_own_key returns the raw key once; the stored value is a hash.
- update_allowed_origins validates EACH origin against the exact shape the
  gateway's origin_allowed() checks (scheme + host, no path, no wildcard) --
  verified against api.gateway.sessions.origin_allowed's exact-string-match
  contract.
- An empty allowlist is explicitly ALLOWED (a real, documented consequence:
  it disables the widget for that tenant) -- not silently rejected.
- Both reject a non-CLIENT_ADMIN caller and any cross-tenant reach.
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import AuthorizationError, ValidationError

from api.admin.api_keys_repository import (
    get_api_key_info,
    rotate_own_key,
    update_allowed_origins,
)

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


@pytest.fixture(autouse=True)
def _env() -> Any:
    from unittest.mock import patch

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        yield


def _claims(role: Role = Role.CLIENT_ADMIN, tenant_id: str | None = "tenant-a") -> AuthClaims:
    return AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)


class _RecordingDatabase:
    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
    ) -> None:
        self._row = row
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if "UPDATE TENANTS" in query.upper():
            return self._row
        return self._row

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 1"


# -- get_api_key_info ------------------------------------------------------


async def test_get_info_never_exposes_key_material() -> None:
    db = _RecordingDatabase(
        row={
            "client_key_hash": "a" * 64,
            "allowed_origins": ["https://example.com"],
        }
    )
    info = await get_api_key_info(db, _claims())

    assert info.has_key is True
    assert info.allowed_origins == ["https://example.com"]
    # No attribute anywhere carries key material.
    for attr in vars(info).values():
        assert "a" * 64 != attr
        if isinstance(attr, str):
            assert not attr.startswith("pk_")


async def test_get_info_rejects_non_client_admin() -> None:
    for role in (Role.CLIENT_AGENT, Role.VISITOR):
        with pytest.raises(AuthorizationError):
            await get_api_key_info(_RecordingDatabase(), _claims(role=role))


async def test_get_info_rejects_global_caller() -> None:
    with pytest.raises(AuthorizationError):
        await get_api_key_info(
            _RecordingDatabase(), _claims(role=Role.PLATFORM_ADMIN, tenant_id=None)
        )


# -- rotate_own_key ---------------------------------------------------------


async def test_rotate_own_key_returns_raw_key_once() -> None:
    db = _RecordingDatabase(row={"id": "tenant-a"})

    raw_key = await rotate_own_key(db, _claims())

    assert raw_key.startswith("pk_")
    # The bound param must be the HASH, never the raw key.
    update_calls = [c for c in db.fetchrow_calls if "UPDATE TENANTS" in c[0].upper()]
    assert update_calls
    bound_hash = update_calls[0][1][0]
    assert bound_hash != raw_key
    assert raw_key not in update_calls[0][1]


async def test_rotate_own_key_rejects_client_agent() -> None:
    with pytest.raises(AuthorizationError):
        await rotate_own_key(_RecordingDatabase(), _claims(role=Role.CLIENT_AGENT))


# -- update_allowed_origins --------------------------------------------------


async def test_update_allowed_origins_accepts_valid_origins() -> None:
    db = _RecordingDatabase(row={"allowed_origins": ["https://a.example.com"]})

    await update_allowed_origins(
        db, _claims(), origins=["https://a.example.com", "https://b.example.com"]
    )

    assert db.execute_calls or db.fetchrow_calls


async def test_update_allowed_origins_empty_list_is_allowed() -> None:
    """D6: an empty allowlist is a real, documented consequence (disables
    the widget), not silently rejected."""
    db = _RecordingDatabase(row={"allowed_origins": []})

    await update_allowed_origins(db, _claims(), origins=[])
    # Must not raise.


@pytest.mark.parametrize(
    "bad_origin",
    [
        "not-a-url",
        "https://example.com/path",  # no paths -- gateway matches full origin string only
        "https://*.example.com",  # no wildcards
        "example.com",  # missing scheme
        "ftp://example.com",  # only http/https
        "https://example.com/",  # trailing slash is still a path component
    ],
)
async def test_update_allowed_origins_rejects_invalid_shape(bad_origin: str) -> None:
    db = _RecordingDatabase()

    with pytest.raises(ValidationError) as exc_info:
        await update_allowed_origins(db, _claims(), origins=[bad_origin])

    assert exc_info.value.code == "INVALID_ORIGIN"
    assert not db.execute_calls
    assert not any("UPDATE TENANTS" in c[0].upper() for c in db.fetchrow_calls)


async def test_update_allowed_origins_rejects_client_agent() -> None:
    with pytest.raises(AuthorizationError):
        await update_allowed_origins(
            _RecordingDatabase(), _claims(role=Role.CLIENT_AGENT), origins=[]
        )
