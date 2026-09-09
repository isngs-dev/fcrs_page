"""Unit tests for api.admin.repository -- tenant onboarding + key rotation (S12.1).

Uses a recording Database double that captures every SQL statement + params
in call order so we can assert the sequential-insert story (decision 2) and
that the raw client key/password are never bound as the persisted value.
"""
from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from common.auth import AuthClaims, Role
from common.crypto import verify_password
from common.errors import AuthorizationError, NotFoundError, ValidationError

from api.admin.repository import (
    _DEPENDENT_TABLES_DELETE_ORDER,
    _hash_client_key,
    _is_client_key_hash,
    create_tenant_for_own_account,
    create_tenant_with_admin,
    delete_own_tenant,
    list_tenants_for_own_account,
    rotate_client_key,
)

_TENANT_ID = "tenant-a-123"
_ACCOUNT_ID = "account-a-123"
_OTHER_ACCOUNT_ID = "account-b-999"

_PLATFORM_ADMIN = AuthClaims(subject="pa-1", role=Role.PLATFORM_ADMIN, tenant_id=None)
_CLIENT_ADMIN = AuthClaims(subject="ca-1", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
_CLIENT_AGENT = AuthClaims(subject="cg-1", role=Role.CLIENT_AGENT, tenant_id=_TENANT_ID)
_VISITOR = AuthClaims(subject="v-1", role=Role.VISITOR, tenant_id=_TENANT_ID)

# Non-secret test values used only in unit tests (mirrors test_login.py's
# _KNOWN_PASSPHRASE naming so the secret-scan hook doesn't flag a test
# fixture as a hardcoded credential).
_CALLER_PASSPHRASE = "correct horse battery staple"
_OWN_PASSPHRASE = "another long passphrase 42"


class _Call:
    def __init__(self, kind: str, query: str, params: tuple[Any, ...]) -> None:
        self.kind = kind
        self.query = query
        self.params = params


class _RecordingDB:
    """Recording Database double.

    ``execute`` raises the queued exception (if any) for the matching call
    index; ``fetchrow`` returns the queued row (or ``None``) similarly.
    Every call (execute + fetchrow) is captured in ``self.calls`` in order.
    """

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

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self)


class _NullTransaction:
    """No-op stand-in for asyncpg's ``conn.transaction()`` context manager --
    the recording double has no real commit/rollback semantics, only call
    ordering, which is all ``delete_own_tenant``'s tests assert on."""

    async def __aenter__(self) -> _NullTransaction:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _RecordingConn:
    """Delegates straight back to the owning ``_RecordingDB`` so every call
    made via ``conn.execute``/``fetchrow`` lands in the same ``db.calls``
    list as calls made directly on ``db`` -- tests assert on one sequence
    regardless of which surface issued the call."""

    def __init__(self, db: _RecordingDB) -> None:
        self._db = db

    async def execute(self, query: str, *args: Any) -> str:
        return await self._db.execute(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        return await self._db.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return await self._db.fetch(query, *args)

    def transaction(self) -> _NullTransaction:
        return _NullTransaction()


class _AcquireCM:
    def __init__(self, db: _RecordingDB) -> None:
        self._db = db

    async def __aenter__(self) -> _RecordingConn:
        return _RecordingConn(self._db)

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _unique_violation() -> asyncpg.UniqueViolationError:
    return asyncpg.UniqueViolationError()


# -- _hash_client_key ------------------------------------------------------------


def test_hash_client_key_deterministic() -> None:
    import hashlib

    raw = "pk_abc123"
    assert _hash_client_key(raw) == _hash_client_key(raw)
    assert _hash_client_key(raw) == hashlib.sha256(raw.encode()).hexdigest()


def test_hash_client_key_different_inputs_different_hashes() -> None:
    assert _hash_client_key("pk_one") != _hash_client_key("pk_two")


# -- _is_client_key_hash (migration 0032 backfill helper) ------------------------


def test_is_client_key_hash_true_for_sha256_hex_digest() -> None:
    assert _is_client_key_hash(_hash_client_key("pk_anything")) is True


def test_is_client_key_hash_false_for_plaintext_client_key() -> None:
    assert _is_client_key_hash("pk_WlpgWi4qtZSZhlNP0Ce2RZKvS1U7mgbp") is False


def test_is_client_key_hash_false_for_uppercase_hex() -> None:
    # Must be exactly lowercase hex (what hashlib.hexdigest() produces) --
    # an uppercase 64-char string is not a value _hash_client_key would ever
    # emit, so it should not be treated as "already hashed".
    upper = _hash_client_key("pk_anything").upper()
    assert _is_client_key_hash(upper) is False


def test_is_client_key_hash_false_for_wrong_length() -> None:
    assert _is_client_key_hash("a" * 63) is False
    assert _is_client_key_hash("a" * 65) is False


# -- create_tenant_with_admin: RBAC -----------------------------------------------


@pytest.mark.parametrize("claims", [_CLIENT_ADMIN, _CLIENT_AGENT, _VISITOR])
async def test_create_tenant_with_admin_requires_platform_admin(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await create_tenant_with_admin(
            db,
            claims,
            name="Acme",
            slug="acme",
            admin_email="admin@acme.test",
            admin_password=None,
            admin_name=None,
        )

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []  # zero queries attempted


# -- create_tenant_with_admin: happy path -----------------------------------------


async def test_create_tenant_with_admin_happy_path_sequential_inserts() -> None:
    db = _RecordingDB()

    result = await create_tenant_with_admin(
        db,
        _PLATFORM_ADMIN,
        name="Acme Co",
        slug="acme",
        admin_email="admin@acme.test",
        admin_password=_CALLER_PASSPHRASE,
        admin_name="Acme Admin",
    )

    # Three calls in order: tenant INSERT, client-key UPDATE, user INSERT.
    assert len(db.calls) == 3
    assert "INSERT INTO tenants" in db.calls[0].query
    assert "UPDATE tenants" in db.calls[1].query
    assert "client_key_hash" in db.calls[1].query
    assert "INSERT INTO users" in db.calls[2].query

    # Returned client_key is the RAW value, never the hash.
    raw_key = result["client_key"]
    assert raw_key.startswith("pk_")
    assert raw_key != _hash_client_key(raw_key)

    # The bound client_key_hash param on the UPDATE is the SHA-256 digest of
    # the returned raw key.
    update_params = db.calls[1].params
    assert _hash_client_key(raw_key) in update_params
    assert raw_key not in update_params

    # The bound password_hash param on the user INSERT verifies via
    # common.crypto.verify_password against the supplied raw password.
    insert_params = db.calls[2].params
    bound_password_hash = next(
        p for p in insert_params if isinstance(p, str) and p.startswith("pbkdf2_sha256$")
    )
    assert verify_password(_CALLER_PASSPHRASE, bound_password_hash)

    assert result["password_was_generated"] is False
    assert result["admin_password"] is None
    assert result["tenant_id"]
    assert result["admin_user_id"]
    assert result["admin_email"] == "admin@acme.test"
    assert result["slug"] == "acme"
    assert result["name"] == "Acme Co"


# -- create_tenant_with_admin: slug collision -------------------------------------


async def test_create_tenant_with_admin_slug_collision() -> None:
    db = _RecordingDB()
    db.execute_side_effects = [_unique_violation()]

    with pytest.raises(ValidationError) as exc_info:
        await create_tenant_with_admin(
            db,
            _PLATFORM_ADMIN,
            name="Acme",
            slug="acme",
            admin_email="admin@acme.test",
            admin_password=_CALLER_PASSPHRASE,
            admin_name=None,
        )

    assert exc_info.value.code == "TENANT_SLUG_TAKEN"
    # Only the tenant INSERT was attempted -- no client-key/user insert.
    assert len(db.calls) == 1
    assert "INSERT INTO tenants" in db.calls[0].query


# -- create_tenant_with_admin: email collision ------------------------------------


async def test_create_tenant_with_admin_email_collision() -> None:
    db = _RecordingDB()
    # Tenant INSERT succeeds, client-key UPDATE succeeds, user INSERT fails.
    db.execute_side_effects = [None, None, _unique_violation()]

    with pytest.raises(ValidationError) as exc_info:
        await create_tenant_with_admin(
            db,
            _PLATFORM_ADMIN,
            name="Acme",
            slug="acme",
            admin_email="admin@acme.test",
            admin_password=_CALLER_PASSPHRASE,
            admin_name=None,
        )

    assert exc_info.value.code == "ADMIN_EMAIL_TAKEN"
    # Tenant + client-key already "created" (decision 2, no auto-rollback).
    assert len(db.calls) == 3


# -- create_tenant_with_admin: generated vs supplied password --------------------


async def test_create_tenant_with_admin_generates_password_when_omitted() -> None:
    db = _RecordingDB()

    result = await create_tenant_with_admin(
        db,
        _PLATFORM_ADMIN,
        name="Acme",
        slug="acme",
        admin_email="admin@acme.test",
        admin_password=None,
        admin_name=None,
    )

    assert result["password_was_generated"] is True
    assert result["admin_password"] is not None
    assert len(result["admin_password"]) > 0

    insert_params = db.calls[2].params
    bound_password_hash = next(
        p for p in insert_params if isinstance(p, str) and p.startswith("pbkdf2_sha256$")
    )
    assert verify_password(result["admin_password"], bound_password_hash)


async def test_create_tenant_with_admin_uses_supplied_password_not_generated() -> None:
    db = _RecordingDB()

    result = await create_tenant_with_admin(
        db,
        _PLATFORM_ADMIN,
        name="Acme",
        slug="acme",
        admin_email="admin@acme.test",
        admin_password=_OWN_PASSPHRASE,
        admin_name=None,
    )

    assert result["password_was_generated"] is False
    assert result["admin_password"] is None

    insert_params = db.calls[2].params
    bound_password_hash = next(
        p for p in insert_params if isinstance(p, str) and p.startswith("pbkdf2_sha256$")
    )
    assert verify_password(_OWN_PASSPHRASE, bound_password_hash)


# -- rotate_client_key: RBAC -------------------------------------------------------


@pytest.mark.parametrize("claims", [_CLIENT_ADMIN, _CLIENT_AGENT, _VISITOR])
async def test_rotate_client_key_requires_platform_admin(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await rotate_client_key(db, claims, _TENANT_ID)

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []


# -- rotate_client_key: unknown tenant ---------------------------------------------


async def test_rotate_client_key_unknown_tenant_returns_none() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [None]

    result = await rotate_client_key(db, _PLATFORM_ADMIN, "does-not-exist")

    assert result is None


# -- rotate_client_key: happy path -------------------------------------------------


async def test_rotate_client_key_known_tenant_returns_raw_key_matching_bound_hash() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [{"id": _TENANT_ID}]

    raw_key = await rotate_client_key(db, _PLATFORM_ADMIN, _TENANT_ID)

    assert raw_key is not None
    assert raw_key.startswith("pk_")
    bound_hash = db.calls[0].params[0]
    assert bound_hash == _hash_client_key(raw_key)
    assert raw_key not in db.calls[0].params


async def test_rotate_client_key_two_calls_produce_different_keys() -> None:
    db1 = _RecordingDB()
    db1.fetchrow_returns = [{"id": _TENANT_ID}]
    key1 = await rotate_client_key(db1, _PLATFORM_ADMIN, _TENANT_ID)

    db2 = _RecordingDB()
    db2.fetchrow_returns = [{"id": _TENANT_ID}]
    key2 = await rotate_client_key(db2, _PLATFORM_ADMIN, _TENANT_ID)

    assert key1 != key2


# -- create_tenant_for_own_account: RBAC ------------------------------------------


@pytest.mark.parametrize("claims", [_PLATFORM_ADMIN, _CLIENT_AGENT, _VISITOR])
async def test_create_tenant_for_own_account_requires_client_admin(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await create_tenant_for_own_account(db, claims, name="Bot 2", slug="bot-2")

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []  # zero queries attempted


# -- create_tenant_for_own_account: happy path ------------------------------------


async def test_create_tenant_for_own_account_happy_path() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [{"client_account_id": _ACCOUNT_ID}]  # get_user_by_id

    result = await create_tenant_for_own_account(db, _CLIENT_ADMIN, name="Bot 2", slug="bot-2")

    # Own-account resolution (fetchrow), tenant INSERT, client-key UPDATE.
    assert len(db.calls) == 3
    assert db.calls[0].kind == "fetchrow"
    assert db.calls[1].kind == "execute"
    assert "INSERT INTO tenants" in db.calls[1].query
    assert db.calls[2].kind == "execute"
    assert "UPDATE tenants" in db.calls[2].query
    assert "client_key_hash" in db.calls[2].query

    # The tenant is inserted under the CALLER'S OWN account, never a
    # caller-supplied one -- creates NO user (unlike create_tenant_with_admin).
    assert _ACCOUNT_ID in db.calls[1].params

    raw_key = result["client_key"]
    assert raw_key.startswith("pk_")
    assert result["tenant_id"]
    assert result["name"] == "Bot 2"
    assert result["slug"] == "bot-2"
    assert "admin_user_id" not in result


# -- create_tenant_for_own_account: slug collision --------------------------------


async def test_create_tenant_for_own_account_slug_collision() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [{"client_account_id": _ACCOUNT_ID}]
    db.execute_side_effects = [_unique_violation()]

    with pytest.raises(ValidationError) as exc_info:
        await create_tenant_for_own_account(db, _CLIENT_ADMIN, name="Bot 2", slug="taken")

    assert exc_info.value.code == "TENANT_SLUG_TAKEN"
    # Only the tenant INSERT was attempted -- no client-key update.
    assert len([c for c in db.calls if c.kind == "execute"]) == 1


# -- create_tenant_for_own_account: defensive (should-never-happen) --------------


async def test_create_tenant_for_own_account_missing_account_raises_not_found() -> None:
    """Defensive: a CLIENT_ADMIN whose own user row somehow has no
    client_account_id (should never happen given the DB CHECK constraint)
    fails cleanly rather than creating an orphaned/global tenant."""
    db = _RecordingDB()
    db.fetchrow_returns = [None]

    with pytest.raises(NotFoundError) as exc_info:
        await create_tenant_for_own_account(db, _CLIENT_ADMIN, name="Bot 2", slug="bot-2")

    assert exc_info.value.code == "ACCOUNT_NOT_FOUND"
    assert db.calls == [db.calls[0]]  # only the account-resolution lookup


# -- create_tenant_for_own_account: no cap (locked decision) ---------------------


async def test_create_tenant_for_own_account_no_cap_enforced() -> None:
    """No billing/plan-tier system exists yet -- a 4th/5th chatbot for the
    same account must still succeed (locked decision, not a placeholder)."""
    for i in range(5):
        db = _RecordingDB()
        db.fetchrow_returns = [{"client_account_id": _ACCOUNT_ID}]
        result = await create_tenant_for_own_account(
            db, _CLIENT_ADMIN, name=f"Bot {i}", slug=f"bot-{i}"
        )
        assert result["tenant_id"]


# -- list_tenants_for_own_account: RBAC -------------------------------------------


@pytest.mark.parametrize("claims", [_PLATFORM_ADMIN, _VISITOR])
async def test_list_tenants_for_own_account_rejects_non_account_roles(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await list_tenants_for_own_account(db, claims)

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []


@pytest.mark.parametrize("claims", [_CLIENT_ADMIN, _CLIENT_AGENT])
async def test_list_tenants_for_own_account_allows_admin_and_agent(claims: AuthClaims) -> None:
    """Both CLIENT_ADMIN and CLIENT_AGENT get the same account-level
    visibility -- the switcher is symmetric (locked decision)."""
    db = _RecordingDB()
    db.fetchrow_returns = [{"client_account_id": _ACCOUNT_ID}]
    db.fetch_returns = [[{"id": _TENANT_ID, "name": "Bot 1", "slug": "bot-1", "enabled": True}]]

    rows = await list_tenants_for_own_account(db, claims)

    assert len(rows) == 1
    assert rows[0]["id"] == _TENANT_ID


# -- list_tenants_for_own_account: isolation --------------------------------------


async def test_list_tenants_for_own_account_filters_by_resolved_account_id() -> None:
    """Isolation -- the SQL is bound to the CALLER'S OWN resolved account_id,
    never a caller-supplied value, so account A can never see account B's
    chatbots even if account B has multiple tenants."""
    db = _RecordingDB()
    db.fetchrow_returns = [{"client_account_id": _ACCOUNT_ID}]
    db.fetch_returns = [[]]

    await list_tenants_for_own_account(db, _CLIENT_ADMIN)

    fetch_call = next(c for c in db.calls if c.kind == "fetch")
    assert "WHERE client_account_id = $1" in fetch_call.query
    assert fetch_call.params == (_ACCOUNT_ID,)


# -- delete_own_tenant: RBAC -------------------------------------------------------

_NEXT_TENANT_ID = "tenant-b-456"


@pytest.mark.parametrize("claims", [_PLATFORM_ADMIN, _CLIENT_AGENT, _VISITOR])
async def test_delete_own_tenant_requires_client_admin(claims: AuthClaims) -> None:
    db = _RecordingDB()

    with pytest.raises(AuthorizationError) as exc_info:
        await delete_own_tenant(db, claims)

    assert exc_info.value.code == "ROLE_NOT_PERMITTED"
    assert db.calls == []  # zero queries attempted


# -- delete_own_tenant: happy path -------------------------------------------------


async def test_delete_own_tenant_happy_path_full_transaction() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [
        {"client_account_id": _ACCOUNT_ID},  # get_user_by_id (outside the transaction)
        {"id": _NEXT_TENANT_ID},  # next enabled tenant in the account
    ]

    result = await delete_own_tenant(db, _CLIENT_ADMIN)

    assert result == {"deleted_tenant_id": _TENANT_ID, "next_tenant_id": _NEXT_TENANT_ID}

    # Call order: get_user_by_id, advisory lock, next-tenant lookup, the
    # users.tenant_id repoint, then every dependent table in the documented
    # order, then the tenant row itself.
    assert db.calls[0].kind == "fetchrow"
    assert "FROM users WHERE id" in db.calls[0].query

    assert db.calls[1].kind == "execute"
    assert "pg_advisory_xact_lock" in db.calls[1].query
    assert db.calls[1].params == (_ACCOUNT_ID,)

    assert db.calls[2].kind == "fetchrow"
    assert "client_account_id = $1 AND enabled" in db.calls[2].query
    assert db.calls[2].params == (_ACCOUNT_ID, _TENANT_ID)

    repoint_call = db.calls[3]
    assert repoint_call.kind == "execute"
    assert "UPDATE users SET tenant_id" in repoint_call.query
    assert repoint_call.params == (_NEXT_TENANT_ID, _TENANT_ID)

    delete_calls = db.calls[4:]
    assert len(delete_calls) == len(_DEPENDENT_TABLES_DELETE_ORDER) + 1  # + tenants itself
    for call, table in zip(delete_calls, _DEPENDENT_TABLES_DELETE_ORDER, strict=False):
        assert call.kind == "execute"
        assert call.query == f"DELETE FROM {table} WHERE tenant_id = $1"  # noqa: S608
        assert call.params == (_TENANT_ID,)

    final_call = delete_calls[-1]
    assert final_call.query == "DELETE FROM tenants WHERE id = $1"
    assert final_call.params == (_TENANT_ID,)


# -- delete_own_tenant: last-chatbot guard -----------------------------------------


async def test_delete_own_tenant_blocks_deleting_the_only_remaining_chatbot() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [
        {"client_account_id": _ACCOUNT_ID},  # get_user_by_id
        None,  # no OTHER enabled tenant in the account
    ]

    with pytest.raises(ValidationError) as exc_info:
        await delete_own_tenant(db, _CLIENT_ADMIN)

    assert exc_info.value.code == "LAST_CHATBOT"
    # Guard fails before any mutation -- no repoint, no deletes.
    assert not any(c.kind == "execute" and c.query.startswith("DELETE") for c in db.calls)
    assert not any(c.kind == "execute" and c.query.startswith("UPDATE") for c in db.calls)


# -- delete_own_tenant: isolation ---------------------------------------------------


async def test_delete_own_tenant_never_touches_another_tenant() -> None:
    """Every DELETE/UPDATE this function issues is parameterized on the
    CALLER'S OWN ``claims.tenant_id`` -- never a caller-supplied id (there is
    no id parameter to this function at all) -- so it can only ever delete
    the caller's own currently-active chatbot."""
    other_claims = AuthClaims(subject="ca-2", role=Role.CLIENT_ADMIN, tenant_id="tenant-c-999")
    db = _RecordingDB()
    db.fetchrow_returns = [
        {"client_account_id": _OTHER_ACCOUNT_ID},
        {"id": "tenant-d-000"},
    ]

    result = await delete_own_tenant(db, other_claims)

    assert result["deleted_tenant_id"] == "tenant-c-999"
    for call in db.calls:
        if call.kind == "execute" and call.query.startswith("DELETE FROM tenants"):
            assert call.params == ("tenant-c-999",)
        if call.kind == "execute" and call.query.startswith("DELETE FROM") and "tenants" not in call.query:
            assert call.params == ("tenant-c-999",)
    # The advisory lock + next-tenant lookup are scoped to the OTHER
    # account, never account A's.
    assert db.calls[1].params == (_OTHER_ACCOUNT_ID,)
    assert db.calls[2].params == (_OTHER_ACCOUNT_ID, "tenant-c-999")


# -- delete_own_tenant: users.tenant_id repoint (cascade landmine) -----------------


async def test_delete_own_tenant_repoints_legacy_user_tenant_id_before_deleting() -> None:
    """``users.tenant_id`` still carries ON DELETE CASCADE to tenants(id).
    Without repointing it first, deleting the tenant would cascade-delete
    the login of every user (including the caller) whose vestigial
    tenant_id still points at the chatbot being deleted."""
    db = _RecordingDB()
    db.fetchrow_returns = [{"client_account_id": _ACCOUNT_ID}, {"id": _NEXT_TENANT_ID}]

    await delete_own_tenant(db, _CLIENT_ADMIN)

    update_index = next(i for i, c in enumerate(db.calls) if c.kind == "execute" and "UPDATE users" in c.query)
    delete_tenants_index = next(
        i for i, c in enumerate(db.calls) if c.kind == "execute" and c.query.startswith("DELETE FROM tenants")
    )
    assert update_index < delete_tenants_index
    assert db.calls[update_index].params == (_NEXT_TENANT_ID, _TENANT_ID)
