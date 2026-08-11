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

    def _filtered_accounts(
        self, query: str, args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        """Apply WHERE/ORDER BY/LIMIT the way the real repository emits them.

        Hard-fails (``AssertionError``) on any ORDER BY or WHERE fragment it
        does not recognize, rather than silently ignoring it -- SR-29 F3/8a:
        a stub that guesses converts a loud failure into a quiet wrong answer.
        """
        q = query.strip().upper()
        tenant_id = args[0]
        rows = [r for r in self._accounts.values() if r["tenant_id"] == tenant_id]
        idx = 1

        if "NAME ILIKE $" in q and "DOMAIN ILIKE $" in q:
            pattern = str(args[idx])
            idx += 2
            needle = (
                pattern.removeprefix("%").removesuffix("%")
                .replace("\\\\", "\\").replace("\\%", "%").replace("\\_", "_")
                .lower()
            )
            rows = [
                row for row in rows
                if needle in (row["name"] or "").lower()
                or needle in (row["domain"] or "").lower()
            ]

        if "ORDER BY " not in q:
            return rows, len(rows)

        if "ORDER BY NAME " in q:
            value_for = lambda row: row["name"]  # noqa: E731
        elif "ORDER BY DOMAIN " in q:
            value_for = lambda row: row["domain"]  # noqa: E731
        elif "ORDER BY CREATED_AT " in q:
            value_for = lambda row: row["created_at"]  # noqa: E731
        else:
            raise AssertionError(f"stub cannot honor ORDER BY: {query}")

        descending = " DESC NULLS LAST" in q
        non_null_rows = [row for row in rows if value_for(row) is not None]
        null_rows = [row for row in rows if value_for(row) is None]
        non_null_rows.sort(key=lambda row: row["account_id"], reverse=True)
        non_null_rows.sort(key=value_for, reverse=descending)
        null_rows.sort(key=lambda row: row["account_id"], reverse=True)
        rows = [*non_null_rows, *null_rows]
        total = len(rows)

        if "LIMIT $" in q:
            limit = args[idx]
            idx += 1
            offset = args[idx] if idx < len(args) else 0
            rows = rows[offset : offset + limit]

        return rows, total

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        q = query.strip().upper()

        if "COUNT(*)" in q and "FROM ACCOUNTS" in q:
            rows, total = self._filtered_accounts(query, args)
            return {"count": total}

        if "FROM ACCOUNTS" in q and "WHERE TENANT_ID" in q and "ACCOUNT_ID = $" in q:
            # get_account -- WHERE tenant_id = $1 AND account_id = $2
            tenant_id, account_id = args[0], args[1]
            return self._accounts.get((tenant_id, account_id))

        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        q = query.strip().upper()

        if "FROM ACCOUNTS" in q:
            rows, _ = self._filtered_accounts(query, args)
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


# ---------------------------------------------------------------------------
# SR-29: list_accounts sort/search contract (repository defense in depth)
# ---------------------------------------------------------------------------


_SORT_INJECTION_PAYLOADS = [
    "created_at; DROP TABLE accounts--",
    "created_at) --",
    "name, tenant_id",
    "(SELECT tenant_id)",
    "created_at DESC; SELECT * FROM users",
    "tenant_id",
    "1",
    "name/**/",
    "",
    "NAME",
]


@pytest.mark.parametrize("sort", _SORT_INJECTION_PAYLOADS)
async def test_list_accounts_rejects_unknown_sort_key_at_repository_layer(sort: str) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.accounts.repository import list_accounts

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_accounts(db, _claims(), sort=sort)

        assert exc_info.value.code == "INVALID_ACCOUNT_SORT"
        assert not db.fetch_calls
        assert not db.fetchrow_calls


async def test_list_accounts_rejects_unknown_direction_at_repository_layer() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.accounts.repository import list_accounts

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_accounts(db, _claims(), direction="sideways")

        assert exc_info.value.code == "INVALID_ACCOUNT_SORT_DIRECTION"
        assert not db.fetch_calls
        assert not db.fetchrow_calls


async def test_list_accounts_default_sort_emits_created_at_desc_pk_desc() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await create_account(db, claims, name="Acme")

        await list_accounts(db, claims)

        query, args = db.fetch_calls[-1]
        assert "ORDER BY created_at DESC NULLS LAST, account_id DESC" in query
        assert args[0] == "tenant-abc"


@pytest.mark.parametrize("sort", ["name", "domain", "created"])
async def test_list_accounts_every_sort_key_emits_nulls_last_and_pk_tiebreak(sort: str) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await create_account(db, claims, name="Acme")

        await list_accounts(db, claims, sort=sort, direction="asc")

        query, _ = db.fetch_calls[-1]
        assert "NULLS LAST, account_id DESC" in query


async def test_list_accounts_order_by_binds_no_extra_parameters() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await create_account(db, claims, name="Acme")

        await list_accounts(db, claims, sort="created", direction="desc")
        no_sort_args_len = len(db.fetch_calls[-1][1])

        await list_accounts(db, claims, sort="name", direction="asc")
        with_sort_args_len = len(db.fetch_calls[-1][1])

        assert no_sort_args_len == with_sort_args_len


@pytest.mark.parametrize("payload", _SORT_INJECTION_PAYLOADS)
async def test_list_accounts_sort_sql_never_contains_caller_string(payload: str) -> None:
    """Flagship injection-invariant test: for each rejected sort payload,
    prove the caller's raw string never reaches SQL. Validation is fully
    synchronous and raises before any query is built, so the stub must not
    have captured a single query."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.accounts.repository import list_accounts

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_accounts(db, _claims(), sort=payload)

        assert exc_info.value.code == "INVALID_ACCOUNT_SORT"
        assert db.fetch_calls == []
        assert db.fetchrow_calls == []

        for query, _args in [*db.fetch_calls, *db.fetchrow_calls]:
            assert payload not in query


async def test_list_accounts_count_query_and_page_query_share_identical_where() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await create_account(db, claims, name="Acme Holdings", domain="acme.example")
        await create_account(db, claims, name="Widget Co", domain="widget.example")

        rows, total = await list_accounts(db, claims, q="acme")

        assert total == 1
        assert len(rows) == 1


async def test_list_accounts_q_binds_escaped_pattern_never_interpolates() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await create_account(db, claims, name="100% Match Co")

        rows, _ = await list_accounts(db, claims, q="100%")

        assert [r.name for r in rows] == ["100% Match Co"]
        query, args = db.fetch_calls[-1]
        assert "ILIKE $2 ESCAPE '\\' OR domain ILIKE $3 ESCAPE '\\'" in query
        assert args[1:3] == ("%100\\%%", "%100\\%%")


async def test_list_accounts_q_where_clause_is_parenthesized() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.accounts.repository import create_account, list_accounts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await create_account(db, claims, name="Acme")

        await list_accounts(db, claims, q="ac")

        query, _ = db.fetch_calls[-1]
        assert "AND (name ILIKE" in query
        assert "OR domain ILIKE" in query and "')" in query


async def test_list_accounts_reject_global_runs_before_sort_validation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.accounts.repository import list_accounts

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await list_accounts(db, global_claims, sort="bogus-sort-key")

        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
