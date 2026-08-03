"""Unit tests for api.orchestrator.config_repository (S10.2).

Covers:
- get_orchestrator_config returns the row's thresholds when present.
- get_orchestrator_config returns settings defaults when no row (never None).
- get_orchestrator_config rejects a global (PLATFORM_ADMIN) caller.
- upsert_orchestrator_config binds tenant_id + thresholds positionally,
  ON CONFLICT (tenant_id) DO UPDATE.
- upsert_orchestrator_config validates the ordering invariant
  (INVALID_ORCHESTRATOR_THRESHOLDS) and rejects a global caller.
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.config import get_api_settings
from api.orchestrator.config_repository import (
    OrchestratorConfig,
    get_orchestrator_config,
    upsert_orchestrator_config,
)


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

    async def close(self) -> None:
        pass


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


# -- get_orchestrator_config -----------------------------------------------------


async def test_get_returns_row_thresholds_when_present() -> None:
    db = _RecordingDatabase(
        rows=[
            {
                "answer_threshold": 0.7,
                "escalate_threshold": 0.4,
                "turn_cap": 8,
                "identity_gate_enabled": True,
            }
        ]
    )
    claims = _claims("tenant-a")

    cfg = await get_orchestrator_config(db, claims)

    assert cfg == OrchestratorConfig(
        answer_threshold=0.7, escalate_threshold=0.4, turn_cap=8, identity_gate_enabled=True,
    )
    assert db.last_params[0] == "tenant-a"
    assert "tenant_orchestrator_configs" in db.last_sql.lower()


async def test_get_returns_settings_defaults_when_no_row() -> None:
    """Never returns None -- unconfigured tenant is deterministic via settings."""
    db = _RecordingDatabase(rows=[])
    claims = _claims("tenant-a")
    settings = get_api_settings()

    cfg = await get_orchestrator_config(db, claims)

    assert cfg is not None
    assert cfg.answer_threshold == settings.orchestrator_default_answer_threshold
    assert cfg.escalate_threshold == settings.orchestrator_default_escalate_threshold
    assert cfg.turn_cap == settings.orchestrator_default_turn_cap
    assert cfg.identity_gate_enabled is False


async def test_get_returns_settings_default_turn_cap_when_row_turn_cap_null() -> None:
    """A row with turn_cap IS NULL (an S10.2-era row predating the column) ->
    the settings default, never None -- same resolution as no row at all."""
    db = _RecordingDatabase(
        rows=[
            {
                "answer_threshold": 0.7,
                "escalate_threshold": 0.4,
                "turn_cap": None,
                "identity_gate_enabled": None,
            }
        ]
    )
    claims = _claims("tenant-a")
    settings = get_api_settings()

    cfg = await get_orchestrator_config(db, claims)

    assert cfg.answer_threshold == 0.7
    assert cfg.escalate_threshold == 0.4
    assert cfg.turn_cap == settings.orchestrator_default_turn_cap
    assert cfg.identity_gate_enabled is False


async def test_get_returns_identity_gate_off_when_row_column_null() -> None:
    """A row with identity_gate_enabled IS NULL (an S10.4-era row predating
    this column, SR-14) -> False, never None -- default-OFF (D9), same
    resolution as no row at all."""
    db = _RecordingDatabase(
        rows=[
            {
                "answer_threshold": 0.7,
                "escalate_threshold": 0.4,
                "turn_cap": 6,
                "identity_gate_enabled": None,
            }
        ]
    )
    claims = _claims("tenant-a")

    cfg = await get_orchestrator_config(db, claims)

    assert cfg.identity_gate_enabled is False


async def test_get_returns_identity_gate_on_when_explicitly_true() -> None:
    db = _RecordingDatabase(
        rows=[
            {
                "answer_threshold": 0.7,
                "escalate_threshold": 0.4,
                "turn_cap": 6,
                "identity_gate_enabled": True,
            }
        ]
    )
    claims = _claims("tenant-a")

    cfg = await get_orchestrator_config(db, claims)

    assert cfg.identity_gate_enabled is True


async def test_get_rejects_global_caller() -> None:
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_orchestrator_config(db, claims)


# -- upsert_orchestrator_config ---------------------------------------------------


async def test_upsert_binds_tenant_and_thresholds_positionally() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    await upsert_orchestrator_config(
        db, claims, answer_threshold=0.6, escalate_threshold=0.3,
    )

    assert db.last_params == ("tenant-a", 0.6, 0.3, None, None)
    assert "ON CONFLICT (tenant_id)" in db.last_sql
    assert "tenant_orchestrator_configs" in db.last_sql.lower()


async def test_upsert_binds_turn_cap_positionally() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    await upsert_orchestrator_config(
        db, claims, answer_threshold=0.6, escalate_threshold=0.3, turn_cap=8,
    )

    assert db.last_params == ("tenant-a", 0.6, 0.3, 8, None)
    assert "turn_cap" in db.last_sql


async def test_upsert_binds_identity_gate_enabled_positionally() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    await upsert_orchestrator_config(
        db, claims, answer_threshold=0.6, escalate_threshold=0.3, identity_gate_enabled=True,
    )

    assert db.last_params == ("tenant-a", 0.6, 0.3, None, True)
    assert "identity_gate_enabled" in db.last_sql


async def test_upsert_identity_gate_enabled_none_clears_to_default() -> None:
    """identity_gate_enabled=None is a valid, explicit clear-to-default
    (False/OFF) -- always bound, mirroring turn_cap=None."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    await upsert_orchestrator_config(
        db, claims, answer_threshold=0.6, escalate_threshold=0.3, identity_gate_enabled=None,
    )

    assert db.last_params == ("tenant-a", 0.6, 0.3, None, None)


async def test_upsert_rejects_turn_cap_below_one() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    with pytest.raises(ValidationError) as exc_info:
        await upsert_orchestrator_config(
            db, claims, answer_threshold=0.6, escalate_threshold=0.3, turn_cap=0,
        )
    assert exc_info.value.code == "INVALID_TURN_CAP"


async def test_upsert_turn_cap_none_clears_to_default() -> None:
    """turn_cap=None is a valid, explicit clear-to-default -- always bound."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    await upsert_orchestrator_config(
        db, claims, answer_threshold=0.6, escalate_threshold=0.3, turn_cap=None,
    )

    assert db.last_params == ("tenant-a", 0.6, 0.3, None, None)


async def test_upsert_rejects_escalate_greater_than_answer() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    with pytest.raises(ValidationError) as exc_info:
        await upsert_orchestrator_config(
            db, claims, answer_threshold=0.3, escalate_threshold=0.6,
        )

    assert exc_info.value.code == "INVALID_ORCHESTRATOR_THRESHOLDS"


async def test_upsert_rejects_out_of_range_thresholds() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    with pytest.raises(ValidationError) as exc_info:
        await upsert_orchestrator_config(
            db, claims, answer_threshold=1.5, escalate_threshold=0.3,
        )
    assert exc_info.value.code == "INVALID_ORCHESTRATOR_THRESHOLDS"

    with pytest.raises(ValidationError) as exc_info2:
        await upsert_orchestrator_config(
            db, claims, answer_threshold=0.5, escalate_threshold=-0.1,
        )
    assert exc_info2.value.code == "INVALID_ORCHESTRATOR_THRESHOLDS"


async def test_upsert_allows_equal_thresholds() -> None:
    """escalate == answer is a valid (collapsed-band) config."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a")

    await upsert_orchestrator_config(
        db, claims, answer_threshold=0.5, escalate_threshold=0.5,
    )
    assert db.last_params == ("tenant-a", 0.5, 0.5, None, None)


async def test_upsert_rejects_global_caller() -> None:
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await upsert_orchestrator_config(
            db, claims, answer_threshold=0.5, escalate_threshold=0.3,
        )
