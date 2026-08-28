"""Unit tests for the calls repository (missed-call text-back config).

Covers:
- upsert_call_config + get_call_config round-trip, scoped to the caller's tenant.
- get_call_config_by_tenant_id: the claims-less webhook read.
- MANDATORY tenant isolation: tenant A's config is invisible to tenant B.
- Global caller (PLATFORM_ADMIN) -> ValidationError on the claims-scoped methods.
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.calls.repository import get_call_config, get_call_config_by_tenant_id, upsert_call_config

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


class _StubDatabase:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self.last_sql: str = ""
        self.last_params: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.last_sql = query
        self.last_params = args
        return self._rows.get(args[0])

    async def execute(self, query: str, *args: Any) -> str:
        self.last_sql = query
        self.last_params = args
        tenant_id, monitored_phone_number, enabled, text_back_message = args
        self._rows[tenant_id] = {
            "monitored_phone_number": monitored_phone_number,
            "enabled": enabled,
            "text_back_message": text_back_message,
        }
        return "INSERT 1"


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)


async def test_upsert_then_get_round_trip() -> None:
    db = _StubDatabase()
    claims = _claims(_TENANT_A)

    await upsert_call_config(
        db, claims,
        monitored_phone_number="+15005550006",
        enabled=True,
        text_back_message="Sorry we missed your call!",
    )
    config = await get_call_config(db, claims)

    assert config is not None
    assert config.monitored_phone_number == "+15005550006"
    assert config.enabled is True
    assert config.text_back_message == "Sorry we missed your call!"


async def test_get_returns_none_when_never_configured() -> None:
    db = _StubDatabase()
    config = await get_call_config(db, _claims(_TENANT_A))
    assert config is None


async def test_upsert_uses_the_callers_own_tenant_id() -> None:
    db = _StubDatabase()
    await upsert_call_config(
        db, _claims(_TENANT_A),
        monitored_phone_number="+15005550006", enabled=True, text_back_message="Hi",
    )
    assert db.last_params[0] == _TENANT_A


# -- tenant isolation (MANDATORY) -------------------------------------------------


async def test_tenant_isolation_a_cannot_read_b() -> None:
    db = _StubDatabase()
    await upsert_call_config(
        db, _claims(_TENANT_A),
        monitored_phone_number="+15005550006", enabled=True, text_back_message="A's message",
    )
    await upsert_call_config(
        db, _claims(_TENANT_B),
        monitored_phone_number="+15005550007", enabled=True, text_back_message="B's message",
    )

    config_a = await get_call_config(db, _claims(_TENANT_A))
    config_b = await get_call_config(db, _claims(_TENANT_B))

    assert config_a is not None and config_a.text_back_message == "A's message"
    assert config_b is not None and config_b.text_back_message == "B's message"


# -- get_call_config_by_tenant_id (claims-less webhook read) ---------------------


async def test_get_by_tenant_id_returns_the_matching_tenants_config() -> None:
    db = _StubDatabase()
    await upsert_call_config(
        db, _claims(_TENANT_A),
        monitored_phone_number="+15005550006", enabled=True, text_back_message="A's message",
    )

    config = await get_call_config_by_tenant_id(db, _TENANT_A)

    assert config is not None
    assert config.text_back_message == "A's message"


async def test_get_by_tenant_id_returns_none_for_unknown_tenant() -> None:
    db = _StubDatabase()
    config = await get_call_config_by_tenant_id(db, "never-configured-tenant")
    assert config is None


# -- global caller rejection -------------------------------------------------------


async def test_get_call_config_rejects_global_caller() -> None:
    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await get_call_config(db, _claims(None, role=Role.PLATFORM_ADMIN))
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_upsert_call_config_rejects_global_caller() -> None:
    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await upsert_call_config(
            db, _claims(None, role=Role.PLATFORM_ADMIN),
            monitored_phone_number="+15005550006", enabled=True, text_back_message="Hi",
        )
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
