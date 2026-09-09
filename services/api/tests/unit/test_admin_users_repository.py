"""Unit tests for api.admin.users_repository (S12.2, re-scoped for
multi-chatbot accounts) -- list/invite/deactivate an ACCOUNT's
CLIENT_AGENTs.

Uses a recording Database double (mirrors test_admin_repository.py's
_RecordingDB) so we can assert the bound SQL/params, plus a seed-backed stub
for set_user_active's SELECT-then-UPDATE flow. Every real query is now
preceded by ONE extra ``fetchrow`` call resolving the caller's account via
``get_tenant_for_switch`` (``_require_account_scoped_client_admin``) --
tests queue that return first and shift real-query call indices by one.
"""
from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from common.auth import AuthClaims, Role
from common.crypto import verify_password
from common.errors import AuthorizationError, NotFoundError, ValidationError

from api.admin.users_repository import create_tenant_agent, list_tenant_users, set_user_active

_ACCOUNT_A = "account-a"
_ACCOUNT_B = "account-b"
_TENANT_A1 = "tenant-a1-123"
_TENANT_A2 = "tenant-a2-456"  # a 2nd chatbot, SAME account as _TENANT_A1
_TENANT_B1 = "tenant-b1-999"

_CLIENT_ADMIN = AuthClaims(subject="ca-1", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A1)
# Same account, DIFFERENT currently-active chatbot -- membership must not
# care which one is active.
_CLIENT_ADMIN_ON_A2 = AuthClaims(subject="ca-1", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A2)
_OTHER_CLIENT_ADMIN = AuthClaims(subject="ca-2", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_B1)
_CLIENT_AGENT = AuthClaims(subject="cg-1", role=Role.CLIENT_AGENT, tenant_id=_TENANT_A1)
_VISITOR = AuthClaims(subject="v-1", role=Role.VISITOR, tenant_id=_TENANT_A1)
_PLATFORM_ADMIN = AuthClaims(subject="pa-1", role=Role.PLATFORM_ADMIN, tenant_id=None)


def _tenant_row(tenant_id: str, account_id: str) -> dict[str, Any]:
    """A get_tenant_for_switch-shaped row -- queued FIRST for every call that
    reaches _require_account_scoped_client_admin."""
    return {"id": tenant_id, "name": "Bot", "slug": "bot", "enabled": True, "client_account_id": account_id}


class _Call:
    def __init__(self, kind: str, query: str, params: tuple[Any, ...]) -> None:
        self.kind = kind
        self.query = query
        self.params = params


class _RecordingDB:
    """Recording Database double (mirrors test_admin_repository.py's)."""

    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.execute_side_effects: list[Exception | None] = []
        self.fetchrow_returns: list[dict[str, Any] | None] = []
        self.fetch_returns: list[list[dict[str, Any]]] = []
        self._execute_i = 0
        self._fetchrow_i = 0
        self._fetch_i = 0

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(_Call("execute", query, args))
        if self._execute_i < len(self.execute_side_effects):
            effect = self.execute_side_effects[self._execute_i]
            self._execute_i += 1
            if effect is not None:
                raise effect
        else:
            self._execute_i += 1
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(_Call("fetchrow", query, args))
        if self._fetchrow_i < len(self.fetchrow_returns):
            row = self.fetchrow_returns[self._fetchrow_i]
            self._fetchrow_i += 1
            return row
        self._fetchrow_i += 1
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(_Call("fetch", query, args))
        if self._fetch_i < len(self.fetch_returns):
            rows = self.fetch_returns[self._fetch_i]
            self._fetch_i += 1
            return rows
        self._fetch_i += 1
        return []


def _unique_violation() -> asyncpg.UniqueViolationError:
    return asyncpg.UniqueViolationError()


def _user_row(
    *,
    user_id: str = "user-1",
    tenant_id: str = _TENANT_A1,
    email: str = "agent@acme.test",
    role: str = "CLIENT_AGENT",
    name: str | None = "Agent Smith",
    active: bool = True,
) -> dict[str, Any]:
    return {
        "id": user_id,
        "tenant_id": tenant_id,
        "client_account_id": _ACCOUNT_A,
        "email": email,
        "role": role,
        "name": name,
        "active": active,
        "last_login_at": None,
        "created_at": None,
    }


# -- list_tenant_users -------------------------------------------------------


async def test_list_tenant_users_binds_account_id_and_excludes_password_hash() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A)]
    db.fetch_returns = [[_user_row()]]

    rows = await list_tenant_users(db, _CLIENT_ADMIN)

    assert len(rows) == 1
    assert "password_hash" not in rows[0]
    fetch_call = next(c for c in db.calls if c.kind == "fetch")
    assert "password_hash" not in fetch_call.query.lower().replace("_", "")
    assert _ACCOUNT_A in fetch_call.params
    assert "WHERE client_account_id = $1" in fetch_call.query


async def test_list_tenant_users_same_account_different_active_tenant_binds_same_account() -> None:
    """Isolation-adjacent: the caller's currently-active chatbot must not
    change WHICH account's members are listed -- both tenants belong to
    account A."""
    db1 = _RecordingDB()
    db1.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A)]
    db1.fetch_returns = [[_user_row()]]
    await list_tenant_users(db1, _CLIENT_ADMIN)  # active tenant = A1

    db2 = _RecordingDB()
    db2.fetchrow_returns = [_tenant_row(_TENANT_A2, _ACCOUNT_A)]
    db2.fetch_returns = [[_user_row()]]
    await list_tenant_users(db2, _CLIENT_ADMIN_ON_A2)  # active tenant = A2, SAME account

    bound_1 = next(c for c in db1.calls if c.kind == "fetch").params
    bound_2 = next(c for c in db2.calls if c.kind == "fetch").params
    assert bound_1 == bound_2 == (_ACCOUNT_A,)


async def test_list_tenant_users_cross_account_never_bound_to_others_account() -> None:
    """Isolation -- account B's caller resolving their own tenant must never
    bind account A's id, even though A also exists."""
    db = _RecordingDB()
    db.fetchrow_returns = [_tenant_row(_TENANT_B1, _ACCOUNT_B)]
    db.fetch_returns = [[]]

    await list_tenant_users(db, _OTHER_CLIENT_ADMIN)

    fetch_call = next(c for c in db.calls if c.kind == "fetch")
    assert fetch_call.params == (_ACCOUNT_B,)
    assert _ACCOUNT_A not in fetch_call.params


@pytest.mark.parametrize("claims", [_CLIENT_AGENT, _VISITOR])
async def test_list_tenant_users_rejects_non_client_admin(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await list_tenant_users(db, claims)

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []


async def test_list_tenant_users_rejects_global_caller() -> None:
    db = _RecordingDB()

    with pytest.raises(ValidationError) as exc_info:
        await list_tenant_users(db, _PLATFORM_ADMIN)

    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
    assert db.calls == []


async def test_list_tenant_users_missing_account_raises_not_found() -> None:
    """Defensive: the active tenant somehow has no client_account_id
    (should never happen post-migration) fails cleanly."""
    db = _RecordingDB()
    db.fetchrow_returns = [None]

    with pytest.raises(NotFoundError) as exc_info:
        await list_tenant_users(db, _CLIENT_ADMIN)

    assert exc_info.value.code == "ACCOUNT_NOT_FOUND"


# -- create_tenant_agent ------------------------------------------------------


async def test_create_tenant_agent_hardcodes_role_client_agent() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A)]

    result = await create_tenant_agent(
        db, _CLIENT_ADMIN, email="new-agent@acme.test", name="New Agent"
    )

    insert_call = next(c for c in db.calls if c.kind == "execute")
    assert "INSERT INTO users" in insert_call.query
    params = insert_call.params
    assert "CLIENT_AGENT" in params
    # Bound under the caller's ACCOUNT, not a caller-supplied value.
    assert _ACCOUNT_A in params
    assert result["role"] == "CLIENT_AGENT"
    assert result["tenant_id"] == _TENANT_A1


async def test_create_tenant_agent_invited_from_either_tenant_lands_in_same_account() -> None:
    """Inviting while A1 is active vs. while A2 is active both create a
    member of the SAME account (the account, not the active tenant, is what
    matters)."""
    db1 = _RecordingDB()
    db1.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A)]
    await create_tenant_agent(db1, _CLIENT_ADMIN, email="a@acme.test", name=None)

    db2 = _RecordingDB()
    db2.fetchrow_returns = [_tenant_row(_TENANT_A2, _ACCOUNT_A)]
    await create_tenant_agent(db2, _CLIENT_ADMIN_ON_A2, email="b@acme.test", name=None)

    params_1 = next(c for c in db1.calls if c.kind == "execute").params
    params_2 = next(c for c in db2.calls if c.kind == "execute").params
    assert _ACCOUNT_A in params_1
    assert _ACCOUNT_A in params_2


async def test_create_tenant_agent_generates_and_binds_hashed_temp_password() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A)]

    result = await create_tenant_agent(db, _CLIENT_ADMIN, email="a@acme.test", name=None)

    raw_password = result["temp_password"]
    assert raw_password
    params = next(c for c in db.calls if c.kind == "execute").params
    bound_hash = next(
        p for p in params if isinstance(p, str) and p.startswith("pbkdf2_sha256$")
    )
    assert verify_password(raw_password, bound_hash)
    assert raw_password not in params


async def test_create_tenant_agent_email_collision() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A)]
    db.execute_side_effects = [_unique_violation()]

    with pytest.raises(ValidationError) as exc_info:
        await create_tenant_agent(db, _CLIENT_ADMIN, email="dup@acme.test", name=None)

    assert exc_info.value.code == "ADMIN_EMAIL_TAKEN"


@pytest.mark.parametrize("claims", [_CLIENT_AGENT, _VISITOR])
async def test_create_tenant_agent_rejects_non_client_admin(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await create_tenant_agent(db, claims, email="x@acme.test", name=None)

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []


async def test_create_tenant_agent_rejects_global_caller() -> None:
    db = _RecordingDB()

    with pytest.raises(ValidationError) as exc_info:
        await create_tenant_agent(db, _PLATFORM_ADMIN, email="x@acme.test", name=None)

    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
    assert db.calls == []


# -- set_user_active -----------------------------------------------------------


async def test_set_user_active_missing_returns_none() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A), None]

    result = await set_user_active(db, _CLIENT_ADMIN, "does-not-exist", active=False)

    assert result is None


async def test_set_user_active_cross_account_returns_none() -> None:
    db = _RecordingDB()
    # Account resolution succeeds; the SELECT with the account filter finds
    # nothing for a cross-account id.
    db.fetchrow_returns = [_tenant_row(_TENANT_A1, _ACCOUNT_A), None]

    result = await set_user_active(db, _CLIENT_ADMIN, "user-in-account-b", active=False)

    assert result is None
    select_call = db.calls[1]
    assert select_call.kind == "fetchrow"
    assert _ACCOUNT_A in select_call.params


async def test_set_user_active_self_targeting_raises_invalid_target() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [
        _tenant_row(_TENANT_A1, _ACCOUNT_A),
        _user_row(user_id=_CLIENT_ADMIN.subject, role="CLIENT_ADMIN"),
    ]

    with pytest.raises(ValidationError) as exc_info:
        await set_user_active(db, _CLIENT_ADMIN, _CLIENT_ADMIN.subject, active=False)

    assert exc_info.value.code == "INVALID_TARGET_USER"
    # Account resolution + the SELECT ran -- no UPDATE.
    assert len(db.calls) == 2


async def test_set_user_active_targeting_client_admin_raises_invalid_target() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [
        _tenant_row(_TENANT_A1, _ACCOUNT_A),
        _user_row(user_id="other-admin", role="CLIENT_ADMIN"),
    ]

    with pytest.raises(ValidationError) as exc_info:
        await set_user_active(db, _CLIENT_ADMIN, "other-admin", active=False)

    assert exc_info.value.code == "INVALID_TARGET_USER"
    assert len(db.calls) == 2


async def test_set_user_active_targeting_platform_admin_raises_invalid_target() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [
        _tenant_row(_TENANT_A1, _ACCOUNT_A),
        _user_row(user_id="platform-admin-1", role="PLATFORM_ADMIN"),
    ]

    with pytest.raises(ValidationError) as exc_info:
        await set_user_active(db, _CLIENT_ADMIN, "platform-admin-1", active=False)

    assert exc_info.value.code == "INVALID_TARGET_USER"


async def test_set_user_active_legit_target_succeeds() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [
        _tenant_row(_TENANT_A1, _ACCOUNT_A),
        _user_row(user_id="agent-1", role="CLIENT_AGENT", active=True),
        _user_row(user_id="agent-1", role="CLIENT_AGENT", active=False),
    ]

    result = await set_user_active(db, _CLIENT_ADMIN, "agent-1", active=False)

    assert result is not None
    assert result["active"] is False
    update_call = db.calls[2]
    assert "UPDATE users" in update_call.query
    update_params = update_call.params
    assert False in update_params
    assert "agent-1" in update_params
    assert _ACCOUNT_A in update_params


async def test_set_user_active_reachable_regardless_of_which_account_tenant_is_active() -> None:
    """An agent invited under account A is deactivatable whether the caller
    currently has A1 or A2 active -- membership, not the active chatbot, is
    what's checked."""
    db = _RecordingDB()
    db.fetchrow_returns = [
        _tenant_row(_TENANT_A2, _ACCOUNT_A),
        _user_row(user_id="agent-1", role="CLIENT_AGENT", active=True),
        _user_row(user_id="agent-1", role="CLIENT_AGENT", active=False),
    ]

    result = await set_user_active(db, _CLIENT_ADMIN_ON_A2, "agent-1", active=False)

    assert result is not None
    assert result["active"] is False


@pytest.mark.parametrize("claims", [_CLIENT_AGENT, _VISITOR])
async def test_set_user_active_rejects_non_client_admin(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await set_user_active(db, claims, "agent-1", active=False)

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []


async def test_set_user_active_rejects_global_caller() -> None:
    db = _RecordingDB()

    with pytest.raises(ValidationError) as exc_info:
        await set_user_active(db, _PLATFORM_ADMIN, "agent-1", active=False)

    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
    assert db.calls == []
