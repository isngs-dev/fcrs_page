"""Unit tests for api.opportunities.config_repository (SR-9.4 D3/D7/D10).

Covers (MANDATORY per the sprint spec):
- An unconfigured tenant resolves all 4 defaults (10/25/50/75) and USD --
  get_opportunity_config never returns None.
- A configured tenant's config carries its own values, never the default.
- Config isolation: tenant A's custom probabilities never affect tenant B's
  resolved config (the highest-value config test).
- upsert validation in Python before the DB: INVALID_CURRENCY,
  INVALID_STAGE_PROBABILITIES (unknown key, terminal key, out-of-range
  value).
- _reject_global for both methods.
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

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

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _TEST_ENV.items():
        monkeypatch.setenv(k, v)
    _reset_settings()
    yield
    _reset_settings()


class _StubDatabase:
    """In-memory stub database for tenant_opportunity_configs."""

    def __init__(self) -> None:
        self._configs: dict[str, dict[str, Any]] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANT_OPPORTUNITY_CONFIGS" in q:
            tenant_id = args[0]
            return self._configs.get(tenant_id)
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO TENANT_OPPORTUNITY_CONFIGS"):
            tenant_id, currency, stage_probabilities = args
            self._configs[tenant_id] = {
                "tenant_id": tenant_id,
                "currency": currency,
                "stage_probabilities": stage_probabilities,
            }
            return "INSERT 0 1"
        return "OK"


def _claims(tenant_id: str | None = _TENANT_A, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# get_opportunity_config
# ---------------------------------------------------------------------------


async def test_unconfigured_tenant_resolves_platform_defaults() -> None:
    from api.opportunities.config_repository import get_opportunity_config

    db = _StubDatabase()
    config = await get_opportunity_config(db, _claims())

    assert config.currency == "USD"
    assert config.stage_probabilities == {
        "prospecting": 10,
        "qualification": 25,
        "proposal": 50,
        "negotiation": 75,
    }


async def test_configured_tenant_resolves_its_own_values() -> None:
    from api.opportunities.config_repository import (
        get_opportunity_config,
        upsert_opportunity_config,
    )

    db = _StubDatabase()
    await upsert_opportunity_config(
        db,
        _claims(_TENANT_A),
        currency="GBP",
        stage_probabilities={"prospecting": 5, "qualification": 20, "proposal": 60, "negotiation": 85},
    )

    config = await get_opportunity_config(db, _claims(_TENANT_A))
    assert config.currency == "GBP"
    assert config.stage_probabilities["proposal"] == 60


async def test_config_isolation_tenant_a_does_not_affect_tenant_b() -> None:
    """MANDATORY: the single highest-value config test in this sprint."""
    from api.opportunities.config_repository import (
        get_opportunity_config,
        upsert_opportunity_config,
    )

    db = _StubDatabase()
    await upsert_opportunity_config(
        db,
        _claims(_TENANT_A),
        currency="GBP",
        stage_probabilities={"prospecting": 1, "qualification": 2, "proposal": 90, "negotiation": 99},
    )

    tenant_b_config = await get_opportunity_config(db, _claims(_TENANT_B))
    assert tenant_b_config.currency == "USD"
    assert tenant_b_config.stage_probabilities == {
        "prospecting": 10,
        "qualification": 25,
        "proposal": 50,
        "negotiation": 75,
    }


async def test_get_config_rejects_global_caller() -> None:
    from api.opportunities.config_repository import get_opportunity_config

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await get_opportunity_config(db, _claims(None, Role.PLATFORM_ADMIN))
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# upsert_opportunity_config -- validation
# ---------------------------------------------------------------------------


async def test_upsert_rejects_global_caller() -> None:
    from api.opportunities.config_repository import upsert_opportunity_config

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await upsert_opportunity_config(
            db,
            _claims(None, Role.PLATFORM_ADMIN),
            currency="USD",
            stage_probabilities={},
        )
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


@pytest.mark.parametrize("bad_currency", ["usd", "US", "DOLLARS", "US1", ""])
async def test_upsert_invalid_currency_rejected_before_db(bad_currency: str) -> None:
    from api.opportunities.config_repository import upsert_opportunity_config

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await upsert_opportunity_config(
            db, _claims(), currency=bad_currency, stage_probabilities={},
        )
    assert exc_info.value.code == "INVALID_CURRENCY"
    assert db._configs == {}


async def test_upsert_unknown_stage_key_rejected() -> None:
    from api.opportunities.config_repository import upsert_opportunity_config

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await upsert_opportunity_config(
            db, _claims(), currency="USD", stage_probabilities={"bogus_stage": 50},
        )
    assert exc_info.value.code == "INVALID_STAGE_PROBABILITIES"
    assert db._configs == {}


@pytest.mark.parametrize("terminal_stage", ["closed_won", "closed_lost"])
async def test_upsert_terminal_stage_key_rejected(terminal_stage: str) -> None:
    from api.opportunities.config_repository import upsert_opportunity_config

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await upsert_opportunity_config(
            db, _claims(), currency="USD", stage_probabilities={terminal_stage: 80},
        )
    assert exc_info.value.code == "INVALID_STAGE_PROBABILITIES"
    assert db._configs == {}


@pytest.mark.parametrize("bad_value", [-1, 101, 150])
async def test_upsert_out_of_range_probability_rejected(bad_value: int) -> None:
    from api.opportunities.config_repository import upsert_opportunity_config

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await upsert_opportunity_config(
            db, _claims(), currency="USD", stage_probabilities={"proposal": bad_value},
        )
    assert exc_info.value.code == "INVALID_STAGE_PROBABILITIES"
    assert db._configs == {}


async def test_upsert_valid_config_persists() -> None:
    from api.opportunities.config_repository import upsert_opportunity_config

    db = _StubDatabase()
    await upsert_opportunity_config(
        db,
        _claims(),
        currency="EUR",
        stage_probabilities={"prospecting": 15, "qualification": 30, "proposal": 55, "negotiation": 80},
    )
    assert db._configs[_TENANT_A]["currency"] == "EUR"
