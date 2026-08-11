"""Unit tests for api.admin.workspace_repository (SR-20 D5).

Covers:
- get_workspace never returns None; an unconfigured tenant's timezone
  resolves to the settings default (workspace_default_timezone), never a
  guessed value.
- get_workspace returns the row's stored name/slug/timezone when present.
- update_workspace validates the IANA timezone in Python BEFORE the DB --
  a valid Area/Location id (Europe/London) is accepted; "GMT+0", "BST" and
  garbage are rejected with a specific ValidationError code, and no DB call
  is made for a rejected value.
- update_workspace validates the slug shape in Python before the DB.
- A slug UNIQUE collision at the DB layer is translated to a ValidationError
  with a specific code (422), never left as a raw asyncpg exception.
- Both reject a global (PLATFORM_ADMIN) caller.
- No language column/param anywhere (D5 amended: Language is disabled/
  coming-soon in the UI, not a live stored field -- there is nothing to
  store).
"""
from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.admin.workspace_repository import get_workspace, update_workspace
from api.config import get_api_settings

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


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)


class _RecordingDatabase:
    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        raise_unique_violation: bool = False,
    ) -> None:
        self._row = row
        self._raise_unique_violation = raise_unique_violation
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if "UPDATE TENANTS" in query.upper():
            if self._raise_unique_violation:
                raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
            return self._row
        return self._row

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 1"


# -- get_workspace -------------------------------------------------------


async def test_get_returns_row_values_when_present() -> None:
    db = _RecordingDatabase(
        row={"name": "Acme", "slug": "acme", "timezone": "Europe/London"}
    )
    ws = await get_workspace(db, _claims("tenant-a"))

    assert ws.name == "Acme"
    assert ws.slug == "acme"
    assert ws.timezone == "Europe/London"


async def test_get_unconfigured_timezone_resolves_to_platform_default() -> None:
    """Never None -- an unset timezone resolves to the settings default,
    never a guessed value (no backfill, no silent fabrication)."""
    db = _RecordingDatabase(row={"name": "Acme", "slug": "acme", "timezone": None})
    settings = get_api_settings()

    ws = await get_workspace(db, _claims("tenant-a"))

    assert ws.timezone == settings.workspace_default_timezone
    assert ws.timezone == "UTC"


async def test_get_rejects_global_caller() -> None:
    with pytest.raises(ValidationError) as exc_info:
        await get_workspace(_RecordingDatabase(), _claims(None, role=Role.PLATFORM_ADMIN))
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# -- update_workspace: timezone validation (Python, before the DB) -------


async def test_update_accepts_valid_iana_timezone() -> None:
    db = _RecordingDatabase(row={"name": "Acme", "slug": "acme", "timezone": "Europe/London"})

    await update_workspace(
        db, _claims("tenant-a"), name="Acme", slug="acme", timezone="Europe/London"
    )

    assert db.fetchrow_calls, "expected the UPDATE to run for a valid timezone"


@pytest.mark.parametrize("bad_tz", ["GMT+0", "BST", "not-a-zone", "PST8PDT", ""])
async def test_update_rejects_invalid_timezone_before_db(bad_tz: str) -> None:
    db = _RecordingDatabase()

    with pytest.raises(ValidationError) as exc_info:
        await update_workspace(
            db, _claims("tenant-a"), name="Acme", slug="acme", timezone=bad_tz
        )

    assert exc_info.value.code == "INVALID_TIMEZONE"
    # Validated in Python before ever reaching the DB.
    assert not db.fetchrow_calls
    assert not db.execute_calls


async def test_update_accepts_utc() -> None:
    db = _RecordingDatabase(row={"name": "Acme", "slug": "acme", "timezone": "UTC"})

    await update_workspace(db, _claims("tenant-a"), name="Acme", slug="acme", timezone="UTC")

    assert db.fetchrow_calls


async def test_update_allows_null_timezone() -> None:
    """Explicitly unsetting the timezone (None) is allowed -- resolves back
    to the platform default on the next read."""
    db = _RecordingDatabase(row={"name": "Acme", "slug": "acme", "timezone": None})

    await update_workspace(db, _claims("tenant-a"), name="Acme", slug="acme", timezone=None)

    assert db.fetchrow_calls


# -- update_workspace: slug validation + collision handling --------------


@pytest.mark.parametrize("bad_slug", ["", "Has Spaces", "UPPER", "-leading-dash", "trailing-", "a" * 64])
async def test_update_rejects_invalid_slug_before_db(bad_slug: str) -> None:
    db = _RecordingDatabase()

    with pytest.raises(ValidationError) as exc_info:
        await update_workspace(
            db, _claims("tenant-a"), name="Acme", slug=bad_slug, timezone=None
        )

    assert exc_info.value.code == "INVALID_SLUG"
    assert not db.fetchrow_calls
    assert not db.execute_calls


async def test_update_slug_collision_returns_422_specific_code_not_500() -> None:
    db = _RecordingDatabase(raise_unique_violation=True)

    with pytest.raises(ValidationError) as exc_info:
        await update_workspace(
            db, _claims("tenant-a"), name="Acme", slug="taken", timezone=None
        )

    assert exc_info.value.code == "WORKSPACE_SLUG_TAKEN"
    assert exc_info.value.http_status == 422


async def test_update_rejects_global_caller() -> None:
    with pytest.raises(ValidationError) as exc_info:
        await update_workspace(
            _RecordingDatabase(), _claims(None, role=Role.PLATFORM_ADMIN),
            name="Acme", slug="acme", timezone=None,
        )
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_update_never_binds_a_language_param() -> None:
    """D5 amended: Language is disabled/coming-soon in the UI, not a live
    stored field. update_workspace must never write a language column."""
    db = _RecordingDatabase(row={"name": "Acme", "slug": "acme", "timezone": None})

    await update_workspace(db, _claims("tenant-a"), name="Acme", slug="acme", timezone=None)

    for query, _params in db.fetchrow_calls:
        assert "LANGUAGE" not in query.upper()
