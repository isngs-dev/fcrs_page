"""Unit tests for api.accounts.repository.

Covers:
- create_account inserts a tenant-scoped row with all fields.
- get_account returns the row mapped to Account, or None if not found.
- Cross-tenant isolation: an account created under tenant A is not visible
  to tenant B via get_account.
- list_accounts pagination/ordering.
- Positional placeholders ($1, $2, ...) are used.
- _reject_global raises ValidationError with code GLOBAL_CALLER_NOT_PERMITTED
  for every method.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from common.auth import AuthClaims, Role

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

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _reset_settings() -> None:
    """Clear settings caches."""
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


class _StubDatabase:
    """In-memory stub database for testing the accounts repository."""

    def __init__(self) -> None:
        # accounts: keyed by (tenant_id, account_id)
        self._accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        q = query.strip().upper()

        if q.startswith("INSERT INTO ACCOUNTS"):
            # args: tenant_id, account_id, name, domain
            tenant_id, account_id, name, domain = args
            self._accounts[(tenant_id, account_id)] = {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "name": name,
                "domain": domain,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            return "INSERT 0 1"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        q = query.strip().upper()

        if "COUNT(*)" in q and "FROM ACCOUNTS" in q:
            tenant_id = args[0]
            rows = [r for r in self._accounts.values() if r["tenant_id"] == tenant_id]
            return {"count": len(rows)}

        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q:
            # get_account -- WHERE tenant_id = $1 AND account_id = $2
            tenant_id, account_id = args[0], args[1]
            return self._accounts.get((tenant_id, account_id))

        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        q = query.strip().upper()

        if "FROM ACCOUNTS" in q:
            tenant_id = args[0]
            rows = [r for r in self._accounts.values() if r["tenant_id"] == tenant_id]
            rows.sort(key=lambda r: (r["created_at"], r["account_id"]), reverse=True)
            if "LIMIT $" in q:
                limit = args[1]
                offset = args[2] if len(args) > 2 else 0
                rows = rows[offset : offset + limit]
            return rows

        return []


@pytest.fixture
def stub_db() -> _StubDatabase:
    return _StubDatabase()


def _claims(tenant_id: str = "tenant-abc", role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="admin-123", role=role, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# create_account
# ---------------------------------------------------------------------------


async def test_create_account_inserts_with_all_fields() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account

        db = _StubDatabase()
        claims = _claims()

        account_id = await create_account(db, claims, name="Acme Ltd", domain="acme.example")

        assert isinstance(account_id, str)
        assert len(account_id) == 32

        insert_query, insert_args = db.execute_calls[0]
        assert "insert into accounts" in insert_query.lower()
        assert insert_args[0] == claims.tenant_id
        assert insert_args[1] == account_id
        assert insert_args[2] == "Acme Ltd"
        assert insert_args[3] == "acme.example"


async def test_create_account_positional_placeholders() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account

        db = _StubDatabase()
        claims = _claims()

        await create_account(db, claims, name="Acme Ltd")

        insert_query, _ = db.execute_calls[0]
        assert "$1" in insert_query
        assert "$4" in insert_query
        assert ":" not in insert_query


async def test_create_account_domain_defaults_none() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, get_account

        db = _StubDatabase()
        claims = _claims()

        account_id = await create_account(db, claims, name="No Domain Co")
        account = await get_account(db, claims, account_id)

        assert account is not None
        assert account.domain is None


async def test_create_account_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.accounts.repository import create_account

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await create_account(db, global_claims, name="Acme Ltd")

        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
        assert db.execute_calls == []


# ---------------------------------------------------------------------------
# get_account
# ---------------------------------------------------------------------------


async def test_get_account_returns_mapped_account() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import Account, create_account, get_account

        db = _StubDatabase()
        claims = _claims()

        account_id = await create_account(db, claims, name="Acme Ltd", domain="acme.example")
        account = await get_account(db, claims, account_id)

        assert isinstance(account, Account)
        assert account.account_id == account_id
        assert account.name == "Acme Ltd"
        assert account.domain == "acme.example"
        assert account.created_at == _NOW
        assert account.updated_at == _NOW


async def test_get_account_returns_none_if_not_found() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import get_account

        db = _StubDatabase()
        claims = _claims()

        account = await get_account(db, claims, "nonexistent-id")

        assert account is None


async def test_get_account_cross_tenant_isolation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, get_account

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        account_id = await create_account(db, claims_a, name="Acme Ltd")

        account = await get_account(db, claims_b, account_id)

        assert account is None


async def test_get_account_positional_placeholders() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import get_account

        db = _StubDatabase()
        claims = _claims()

        await get_account(db, claims, "some-id")

        query, args = db.fetchrow_calls[0]
        assert "$1" in query
        assert "$2" in query
        assert ":" not in query
        assert args[0] == claims.tenant_id
        assert args[1] == "some-id"


async def test_get_account_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.accounts.repository import get_account

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await get_account(db, global_claims, "some-id")

        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# list_accounts
# ---------------------------------------------------------------------------


async def test_list_accounts_tenant_scoping() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        await create_account(db, claims_a, name="Acme A")
        await create_account(db, claims_b, name="Acme B")

        rows_a, total_a = await list_accounts(db, claims_a)
        rows_b, total_b = await list_accounts(db, claims_b)

        assert total_a == 1
        assert rows_a[0].name == "Acme A"
        assert total_b == 1
        assert rows_b[0].name == "Acme B"


async def test_list_accounts_pagination_and_ordering() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        ids = []
        for i in range(5):
            account_id = await create_account(db, claims, name=f"Acme {i}")
            ids.append(account_id)
            db._accounts[(claims.tenant_id, account_id)]["created_at"] = datetime(
                2026, 1, i + 1, tzinfo=UTC
            )

        rows, total = await list_accounts(db, claims, limit=2, offset=1)

        assert total == 5
        assert len(rows) == 2
        # newest first: Acme 4 (Jan 5), Acme 3 (Jan 4), skip offset 1 -> Acme 3, Acme 2
        assert [r.name for r in rows] == ["Acme 3", "Acme 2"]


async def test_list_accounts_uses_positional_placeholders() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        await list_accounts(db, claims, limit=10, offset=0)

        page_query, page_args = db.fetch_calls[-1]
        assert "$1" in page_query
        assert "$2" in page_query
        assert "$3" in page_query
        assert ":" not in page_query
        assert page_args[0] == "tenant-abc"


async def test_list_accounts_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.accounts.repository import list_accounts

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await list_accounts(db, global_claims)

        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
        assert db.fetch_calls == []
        assert db.fetchrow_calls == []
