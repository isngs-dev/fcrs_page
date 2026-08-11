"""Unit tests for api.leads.assignment_config_repository (SR-20 D1/D4).

Covers:
- get_assignment_config never returns None -- an unconfigured tenant
  resolves to the settings default (round_robin_enabled=False).
- get_assignment_config returns the row's values when present.
- upsert_assignment_config binds tenant_id + round_robin_enabled positionally,
  ON CONFLICT (tenant_id) DO UPDATE, and never touches the rotation cursor
  (last_assigned_agent_id) -- that column is owned exclusively by the
  atomic advance-cursor step in assignment.py.
- Both reject a global (PLATFORM_ADMIN) caller.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.config import get_api_settings
from api.leads.assignment_config_repository import (
    AssignmentConfig,
    get_assignment_config,
    upsert_assignment_config,
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
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        yield


class _RecordingDatabase:
    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.last_sql: str = ""
        self.last_params: tuple[Any, ...] = ()
        self._rows = rows or []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.last_sql = query
        self.last_params = args
        return self._rows[0] if self._rows else None

    async def execute(self, query: str, *args: Any) -> str:
        self.last_sql = query
        self.last_params = args
        return "INSERT 1"


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


# -- get_assignment_config ---------------------------------------------------


async def test_get_returns_row_values_when_present() -> None:
    db = _RecordingDatabase(
        rows=[{"round_robin_enabled": True, "last_assigned_agent_id": "agent-1"}]
    )
    claims = _claims("tenant-a")

    cfg = await get_assignment_config(db, claims)

    assert cfg == AssignmentConfig(round_robin_enabled=True, last_assigned_agent_id="agent-1")
    assert db.last_params[0] == "tenant-a"
    assert "tenant_assignment_configs" in db.last_sql.lower()


async def test_get_returns_settings_default_when_no_row() -> None:
    """Never returns None -- an unconfigured tenant is deterministic (D1: default OFF)."""
    db = _RecordingDatabase(rows=[])
    claims = _claims("tenant-a")
    settings = get_api_settings()

    cfg = await get_assignment_config(db, claims)

    assert cfg is not None
    assert cfg.round_robin_enabled == settings.assignment_round_robin_default
    assert cfg.round_robin_enabled is False
    assert cfg.last_assigned_agent_id is None


async def test_get_rejects_global_caller() -> None:
    db = _RecordingDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_assignment_config(db, claims)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# -- upsert_assignment_config -------------------------------------------------


async def test_upsert_binds_tenant_and_flag_on_conflict_update() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    await upsert_assignment_config(db, claims, round_robin_enabled=True)

    assert db.last_params[0] == "tenant-a"
    assert db.last_params[1] is True
    assert "ON CONFLICT" in db.last_sql.upper()
    assert "TENANT_ASSIGNMENT_CONFIGS" in db.last_sql.upper()
    # Never writes last_assigned_agent_id -- that's the rotation cursor,
    # owned exclusively by the atomic advance step in assignment.py.
    assert "LAST_ASSIGNED_AGENT_ID" not in db.last_sql.upper()


async def test_upsert_rejects_global_caller() -> None:
    db = _RecordingDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await upsert_assignment_config(db, claims, round_robin_enabled=True)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
