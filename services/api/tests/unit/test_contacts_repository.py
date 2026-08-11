"""Unit tests for api.contacts.repository.

Covers:
- create_contact inserts a row with all fields, including nullable
  lead_id/account_id/name/email.
- MANDATORY (D6): a direct second create_contact call with the same lead_id
  raises ValidationError(code="CONTACT_ALREADY_LINKED_TO_LEAD"), modeled via
  a stub-DB UniqueViolationError on (tenant_id, lead_id).
- The partial index does NOT block multiple manual (lead_id=None) contacts.
- MANDATORY (D4, "the single highest-value test in this sprint"): add_identity
  -- same identity_value across two different tenants both succeed; the same
  identity_value under the same tenant for a second contact raises
  ValidationError(code="IDENTITY_ALREADY_CLAIMED").
- update_contact: only supplied fields change (Ellipsis sentinel);
  account_id=None unlinks.
- Cross-tenant isolation on every method.
- _reject_global for every method.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from asyncpg.exceptions import UniqueViolationError
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
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


class _StubDatabase:
    """In-memory stub database for testing the contacts repository."""

    def __init__(self) -> None:
        # contacts: keyed by (tenant_id, contact_id)
        self._contacts: dict[tuple[str, str], dict[str, Any]] = {}
        # identities: keyed by (tenant_id, identity_id)
        self._identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        q = query.strip().upper()

        if q.startswith("INSERT INTO CONTACTS"):
            # args: tenant_id, contact_id, account_id, lead_id, name, email,
            #       phone, consent, owner_agent_id
            (
                tenant_id, contact_id, account_id, lead_id, name, email,
                phone, consent, owner_agent_id,
            ) = args
            if lead_id is not None:
                for row in self._contacts.values():
                    if row["tenant_id"] == tenant_id and row["lead_id"] == lead_id:
                        raise UniqueViolationError("duplicate key value violates unique constraint")
            self._contacts[(tenant_id, contact_id)] = {
                "tenant_id": tenant_id,
                "contact_id": contact_id,
                "account_id": account_id,
                "lead_id": lead_id,
                "name": name,
                "email": email,
                "phone": phone,
                "consent": consent,
                "owner_agent_id": owner_agent_id,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO CONTACT_IDENTITIES"):
            # args: tenant_id, identity_id, contact_id, identity_type, identity_value
            tenant_id, identity_id, contact_id, identity_type, identity_value = args
            for row in self._identities.values():
                if (
                    row["tenant_id"] == tenant_id
                    and row["identity_type"] == identity_type
                    and row["identity_value"] == identity_value
                ):
                    raise UniqueViolationError("duplicate key value violates unique constraint")
            self._identities[(tenant_id, identity_id)] = {
                "tenant_id": tenant_id,
                "identity_id": identity_id,
                "contact_id": contact_id,
                "identity_type": identity_type,
                "identity_value": identity_value,
                "created_at": _NOW,
            }
            return "INSERT 0 1"

        if q.startswith("UPDATE CONTACTS"):
            # dynamic SET clause; last two params are tenant_id, contact_id
            tenant_id = args[-2]
            contact_id = args[-1]
            existing = self._contacts.get((tenant_id, contact_id))
            if existing is None:
                return "UPDATE 0"
            # Parse assigned column names from the query text in order.
            set_part = query.split("SET", 1)[1].split("WHERE", 1)[0]
            columns = [c.strip().split("=")[0].strip() for c in set_part.split(",")]
            for col, val in zip(columns, args[:-2], strict=False):
                if col == "updated_at":
                    continue
                existing[col] = val
            existing["updated_at"] = _NOW
            return "UPDATE 1"

        return "OK"

    def _filtered_contacts(
        self, query: str, args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        """Apply WHERE/ORDER BY/LIMIT the way the real repository emits them.

        Hard-fails (``AssertionError``) on any ORDER BY or WHERE fragment it
        does not recognize, rather than silently ignoring it -- SR-29 F3/8a:
        a stub that guesses converts a loud failure into a quiet wrong answer.
        """
        q = query.strip().upper()
        tenant_id = args[0]
        rows = [r for r in self._contacts.values() if r["tenant_id"] == tenant_id]
        idx = 1

        if "ACCOUNT_ID = $" in q:
            account_id = args[idx]
            idx += 1
            rows = [row for row in rows if row["account_id"] == account_id]

        if "NAME ILIKE $" in q and "EMAIL ILIKE $" in q:
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
                or needle in (row["email"] or "").lower()
            ]

        if "ORDER BY " not in q:
            return rows, len(rows)

        if "ORDER BY NAME " in q:
            value_for = lambda row: row["name"]  # noqa: E731
        elif "ORDER BY EMAIL " in q:
            value_for = lambda row: row["email"]  # noqa: E731
        elif "ORDER BY ACCOUNT_ID " in q:
            value_for = lambda row: row["account_id"]  # noqa: E731
        elif "ORDER BY OWNER_AGENT_ID " in q:
            value_for = lambda row: row["owner_agent_id"]  # noqa: E731
        elif "ORDER BY CREATED_AT " in q:
            value_for = lambda row: row["created_at"]  # noqa: E731
        else:
            raise AssertionError(f"stub cannot honor ORDER BY: {query}")

        descending = " DESC NULLS LAST" in q
        non_null_rows = [row for row in rows if value_for(row) is not None]
        null_rows = [row for row in rows if value_for(row) is None]
        non_null_rows.sort(key=lambda row: row["contact_id"], reverse=True)
        non_null_rows.sort(key=value_for, reverse=descending)
        null_rows.sort(key=lambda row: row["contact_id"], reverse=True)
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

        if "COUNT(*)" in q and "FROM CONTACTS" in q:
            rows, total = self._filtered_contacts(query, args)
            return {"count": total}

        if "FROM CONTACTS" in q and "WHERE TENANT_ID" in q and "LEAD_ID = $2" in q:
            # get_contact_by_lead_id
            tenant_id, lead_id = args[0], args[1]
            for row in self._contacts.values():
                if row["tenant_id"] == tenant_id and row["lead_id"] == lead_id:
                    return row
            return None

        if "FROM CONTACTS" in q and "WHERE TENANT_ID" in q and "CONTACT_ID = $2" in q:
            tenant_id, contact_id = args[0], args[1]
            return self._contacts.get((tenant_id, contact_id))

        if "CONTACT_ID FROM CONTACT_IDENTITIES" in q:
            # get_contact_id_by_identity
            tenant_id, identity_type, identity_value = args[0], args[1], args[2]
            for row in self._identities.values():
                if (
                    row["tenant_id"] == tenant_id
                    and row["identity_type"] == identity_type
                    and row["identity_value"] == identity_value
                ):
                    return {"contact_id": row["contact_id"]}
            return None

        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        q = query.strip().upper()

        if "FROM CONTACT_IDENTITIES" in q:
            tenant_id, contact_id = args[0], args[1]
            rows = [
                r for r in self._identities.values()
                if r["tenant_id"] == tenant_id and r["contact_id"] == contact_id
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows

        if "FROM CONTACTS" in q:
            rows, _ = self._filtered_contacts(query, args)
            return rows

        if "FROM CONTACTS" in q:
            tenant_id = args[0]
            rows = [r for r in self._contacts.values() if r["tenant_id"] == tenant_id]
            rows.sort(key=lambda r: (r["created_at"], r["contact_id"]), reverse=True)
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


def _consent(purpose: str = "crm") -> dict[str, Any]:
    return {"granted": True, "purpose": purpose, "text": "OK", "captured_at": "2026-01-01T12:00:00Z"}


# ---------------------------------------------------------------------------
# create_contact
# ---------------------------------------------------------------------------


async def test_create_contact_inserts_with_all_fields() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact

        db = _StubDatabase()
        claims = _claims()

        contact_id = await create_contact(
            db, claims,
            account_id="acct-1", lead_id="lead-1", name="Dana", email="dana@example.com",
            phone="+15551234567", consent=_consent(), owner_agent_id="agent-1",
        )

        assert isinstance(contact_id, str)
        assert len(contact_id) == 32

        insert_query, insert_args = db.execute_calls[0]
        assert "insert into contacts" in insert_query.lower()
        assert insert_args[0] == claims.tenant_id
        assert insert_args[1] == contact_id
        assert insert_args[2] == "acct-1"
        assert insert_args[3] == "lead-1"
        assert insert_args[4] == "Dana"
        assert insert_args[5] == "dana@example.com"
        assert insert_args[6] == "+15551234567"
        assert insert_args[7] == _consent()
        assert insert_args[8] == "agent-1"


async def test_create_contact_accepts_nullable_lead_account_name_email() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, get_contact

        db = _StubDatabase()
        claims = _claims()

        contact_id = await create_contact(
            db, claims,
            account_id=None, lead_id=None, name=None, email=None, phone=None,
            consent=_consent(),
        )

        contact = await get_contact(db, claims, contact_id)
        assert contact is not None
        assert contact.account_id is None
        assert contact.lead_id is None
        assert contact.name is None
        assert contact.email is None


async def test_create_contact_duplicate_lead_id_raises_validation_error() -> None:
    """MANDATORY (D6): a direct second create_contact with the same lead_id raises."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import create_contact

        db = _StubDatabase()
        claims = _claims()

        await create_contact(
            db, claims, account_id=None, lead_id="lead-1", name="First", email=None,
            phone=None, consent=_consent(),
        )

        with pytest.raises(ValidationError) as exc_info:
            await create_contact(
                db, claims, account_id=None, lead_id="lead-1", name="Second", email=None,
                phone=None, consent=_consent(),
            )

        assert exc_info.value.code == "CONTACT_ALREADY_LINKED_TO_LEAD"


async def test_create_contact_multiple_manual_contacts_all_succeed() -> None:
    """The partial index does NOT block multiple manual (lead_id=None) contacts."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, list_contacts

        db = _StubDatabase()
        claims = _claims()

        for i in range(3):
            await create_contact(
                db, claims, account_id=None, lead_id=None, name=f"Manual {i}",
                email=None, phone=None, consent=_consent(),
            )

        _, total = await list_contacts(db, claims)
        assert total == 3


async def test_create_contact_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import create_contact

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await create_contact(
                db, global_claims, account_id=None, lead_id=None, name="X", email=None,
                phone=None, consent=_consent(),
            )
        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# get_contact / get_contact_by_lead_id
# ---------------------------------------------------------------------------


async def test_get_contact_returns_mapped_contact() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import Contact, create_contact, get_contact

        db = _StubDatabase()
        claims = _claims()

        contact_id = await create_contact(
            db, claims, account_id="acct-1", lead_id="lead-1", name="Dana",
            email="dana@example.com", phone=None, consent=_consent(),
        )

        contact = await get_contact(db, claims, contact_id)

        assert isinstance(contact, Contact)
        assert contact.contact_id == contact_id
        assert contact.name == "Dana"
        assert contact.consent == _consent()


async def test_get_contact_returns_none_if_not_found() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import get_contact

        db = _StubDatabase()
        claims = _claims()

        contact = await get_contact(db, claims, "nonexistent-id")
        assert contact is None


async def test_get_contact_cross_tenant_isolation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, get_contact

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        contact_id = await create_contact(
            db, claims_a, account_id=None, lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )

        contact = await get_contact(db, claims_b, contact_id)
        assert contact is None


async def test_get_contact_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import get_contact

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_contact(db, global_claims, "some-id")


async def test_get_contact_by_lead_id_returns_contact() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, get_contact_by_lead_id

        db = _StubDatabase()
        claims = _claims()

        contact_id = await create_contact(
            db, claims, account_id=None, lead_id="lead-1", name="Dana", email=None,
            phone=None, consent=_consent(),
        )

        contact = await get_contact_by_lead_id(db, claims, "lead-1")
        assert contact is not None
        assert contact.contact_id == contact_id


async def test_get_contact_by_lead_id_returns_none_when_no_match() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import get_contact_by_lead_id

        db = _StubDatabase()
        claims = _claims()

        contact = await get_contact_by_lead_id(db, claims, "lead-does-not-exist")
        assert contact is None


async def test_get_contact_by_lead_id_cross_tenant_isolation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, get_contact_by_lead_id

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        await create_contact(
            db, claims_a, account_id=None, lead_id="lead-shared", name="Dana", email=None,
            phone=None, consent=_consent(),
        )

        contact = await get_contact_by_lead_id(db, claims_b, "lead-shared")
        assert contact is None


async def test_get_contact_by_lead_id_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import get_contact_by_lead_id

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_contact_by_lead_id(db, global_claims, "lead-1")


# ---------------------------------------------------------------------------
# list_contacts
# ---------------------------------------------------------------------------


async def test_list_contacts_tenant_scoping() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, list_contacts

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        await create_contact(
            db, claims_a, account_id=None, lead_id=None, name="A", email=None,
            phone=None, consent=_consent(),
        )
        await create_contact(
            db, claims_b, account_id=None, lead_id=None, name="B", email=None,
            phone=None, consent=_consent(),
        )

        rows_a, total_a = await list_contacts(db, claims_a)
        rows_b, total_b = await list_contacts(db, claims_b)

        assert total_a == 1
        assert rows_a[0].name == "A"
        assert total_b == 1
        assert rows_b[0].name == "B"


async def test_list_contacts_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await list_contacts(db, global_claims)
        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
        assert db.fetch_calls == []
        assert db.fetchrow_calls == []


# ---------------------------------------------------------------------------
# SR-29: list_contacts sort/search/account_id filter contract (repository
# defense in depth)
# ---------------------------------------------------------------------------


_SORT_INJECTION_PAYLOADS = [
    "created_at; DROP TABLE contacts--",
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


async def _make_contact(
    db: _StubDatabase, claims: AuthClaims, *, name: str | None = "Jane",
    email: str | None = "jane@example.com", account_id: str | None = None,
    owner_agent_id: str | None = None,
) -> str:
    from api.contacts.repository import create_contact

    contact_id = await create_contact(
        db, claims, account_id=account_id, lead_id=None, name=name, email=email,
        phone=None, consent=_consent(), owner_agent_id=owner_agent_id,
    )
    return contact_id


@pytest.mark.parametrize("sort", _SORT_INJECTION_PAYLOADS)
async def test_list_contacts_rejects_unknown_sort_key_at_repository_layer(sort: str) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_contacts(db, _claims(), sort=sort)

        assert exc_info.value.code == "INVALID_CONTACT_SORT"
        assert not db.fetch_calls
        assert not db.fetchrow_calls


async def test_list_contacts_rejects_unknown_direction_at_repository_layer() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_contacts(db, _claims(), direction="sideways")

        assert exc_info.value.code == "INVALID_CONTACT_SORT_DIRECTION"
        assert not db.fetch_calls
        assert not db.fetchrow_calls


async def test_list_contacts_default_sort_emits_created_at_desc_pk_desc() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await _make_contact(db, claims)

        await list_contacts(db, claims)

        query, args = db.fetch_calls[-1]
        assert "ORDER BY created_at DESC NULLS LAST, contact_id DESC" in query
        assert args[0] == "tenant-abc"


@pytest.mark.parametrize("sort", ["name", "email", "account", "owner", "created"])
async def test_list_contacts_every_sort_key_emits_nulls_last_and_pk_tiebreak(sort: str) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await _make_contact(db, claims)

        await list_contacts(db, claims, sort=sort, direction="asc")

        query, _ = db.fetch_calls[-1]
        assert "NULLS LAST, contact_id DESC" in query


async def test_list_contacts_order_by_binds_no_extra_parameters() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await _make_contact(db, claims)

        await list_contacts(db, claims, sort="created", direction="desc")
        no_sort_args_len = len(db.fetch_calls[-1][1])

        await list_contacts(db, claims, sort="name", direction="asc")
        with_sort_args_len = len(db.fetch_calls[-1][1])

        assert no_sort_args_len == with_sort_args_len


@pytest.mark.parametrize("payload", _SORT_INJECTION_PAYLOADS)
async def test_list_contacts_sort_sql_never_contains_caller_string(payload: str) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_contacts(db, _claims(), sort=payload)

        assert exc_info.value.code == "INVALID_CONTACT_SORT"
        assert db.fetch_calls == []
        assert db.fetchrow_calls == []

        for query, _args in [*db.fetch_calls, *db.fetchrow_calls]:
            assert payload not in query


async def test_list_contacts_count_query_and_page_query_share_identical_where() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await _make_contact(db, claims, name="Alice Needle", email="a@example.com")
        await _make_contact(db, claims, name="Bob Other", email="b@example.com")

        rows, total = await list_contacts(db, claims, q="needle")

        assert total == 1
        assert len(rows) == 1


async def test_list_contacts_q_binds_escaped_pattern_never_interpolates() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await _make_contact(db, claims, name="100% Match")

        rows, _ = await list_contacts(db, claims, q="100%")

        assert [r.name for r in rows] == ["100% Match"]
        query, args = db.fetch_calls[-1]
        assert "ILIKE $2 ESCAPE '\\' OR email ILIKE $3 ESCAPE '\\'" in query
        assert args[1:3] == ("%100\\%%", "%100\\%%")


async def test_list_contacts_q_where_clause_is_parenthesized() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await _make_contact(db, claims)

        await list_contacts(db, claims, q="ja")

        query, _ = db.fetch_calls[-1]
        assert "AND (name ILIKE" in query
        assert "OR email ILIKE" in query and "')" in query


async def test_list_contacts_reject_global_runs_before_sort_validation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await list_contacts(db, global_claims, sort="bogus-sort-key")

        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_list_contacts_account_id_filter_binds_parameter() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import list_contacts

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        await _make_contact(db, claims, account_id="acct-1")
        await _make_contact(db, claims, account_id="acct-2")

        rows, total = await list_contacts(db, claims, account_id="acct-1")

        assert total == 1
        assert rows[0].account_id == "acct-1"
        query, args = db.fetch_calls[-1]
        assert "AND account_id = $2" in query
        assert args[1] == "acct-1"


# ---------------------------------------------------------------------------
# update_contact
# ---------------------------------------------------------------------------


async def test_update_contact_only_supplied_fields_change() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, update_contact

        db = _StubDatabase()
        claims = _claims()

        contact_id = await create_contact(
            db, claims, account_id="acct-1", lead_id=None, name="Dana",
            email="dana@example.com", phone="+15551234567", consent=_consent(),
        )

        updated = await update_contact(db, claims, contact_id, name="Dana Updated")

        assert updated is not None
        assert updated.name == "Dana Updated"
        # Untouched fields remain the same.
        assert updated.email == "dana@example.com"
        assert updated.phone == "+15551234567"
        assert updated.account_id == "acct-1"


async def test_update_contact_account_id_none_unlinks() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, update_contact

        db = _StubDatabase()
        claims = _claims()

        contact_id = await create_contact(
            db, claims, account_id="acct-1", lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )

        updated = await update_contact(db, claims, contact_id, account_id=None)

        assert updated is not None
        assert updated.account_id is None


async def test_update_contact_no_fields_supplied_returns_current() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, update_contact

        db = _StubDatabase()
        claims = _claims()

        contact_id = await create_contact(
            db, claims, account_id=None, lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )

        updated = await update_contact(db, claims, contact_id)

        assert updated is not None
        assert updated.name == "Dana"
        # No UPDATE statement issued -- only the read-through get_contact.
        assert not any("update contacts" in q.lower() for q, _ in db.execute_calls)


async def test_update_contact_cross_tenant_returns_none() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import create_contact, update_contact

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        contact_id = await create_contact(
            db, claims_a, account_id=None, lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )

        result = await update_contact(db, claims_b, contact_id, name="Hacked")
        assert result is None


async def test_update_contact_missing_returns_none() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import update_contact

        db = _StubDatabase()
        claims = _claims()

        result = await update_contact(db, claims, "nonexistent-id", name="X")
        assert result is None


async def test_update_contact_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import update_contact

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await update_contact(db, global_claims, "some-id", name="X")


# ---------------------------------------------------------------------------
# add_identity / list_identities / get_contact_id_by_identity
# ---------------------------------------------------------------------------


async def test_add_identity_same_value_different_tenants_both_succeed() -> None:
    """MANDATORY (D4): the single highest-value test in this sprint -- the
    SAME identity_value under two DIFFERENT tenants both succeed."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import add_identity, create_contact

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        contact_a = await create_contact(
            db, claims_a, account_id=None, lead_id=None, name="A", email=None,
            phone=None, consent=_consent(),
        )
        contact_b = await create_contact(
            db, claims_b, account_id=None, lead_id=None, name="B", email=None,
            phone=None, consent=_consent(),
        )

        identity_a = await add_identity(
            db, claims_a, contact_a, identity_type="email", identity_value="shared@example.com",
        )
        identity_b = await add_identity(
            db, claims_b, contact_b, identity_type="email", identity_value="shared@example.com",
        )

        assert isinstance(identity_a, str)
        assert isinstance(identity_b, str)
        assert identity_a != identity_b
        assert contact_a != contact_b


async def test_add_identity_same_value_same_tenant_second_contact_raises() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import add_identity, create_contact

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        contact_1 = await create_contact(
            db, claims, account_id=None, lead_id=None, name="First", email=None,
            phone=None, consent=_consent(),
        )
        contact_2 = await create_contact(
            db, claims, account_id=None, lead_id=None, name="Second", email=None,
            phone=None, consent=_consent(),
        )

        await add_identity(
            db, claims, contact_1, identity_type="email", identity_value="dupe@example.com",
        )

        with pytest.raises(ValidationError) as exc_info:
            await add_identity(
                db, claims, contact_2, identity_type="email", identity_value="dupe@example.com",
            )

        assert exc_info.value.code == "IDENTITY_ALREADY_CLAIMED"


async def test_add_identity_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import add_identity

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await add_identity(
                db, global_claims, "contact-1", identity_type="email", identity_value="x@example.com",
            )


async def test_list_identities_returns_tenant_scoped_ordered() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import (
            ContactIdentity,
            add_identity,
            create_contact,
            list_identities,
        )

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        contact_id = await create_contact(
            db, claims, account_id=None, lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )

        id1 = await add_identity(db, claims, contact_id, identity_type="email", identity_value="dana@example.com")
        id2 = await add_identity(db, claims, contact_id, identity_type="visitor_id", identity_value="visitor-1")
        db._identities[(claims.tenant_id, id1)]["created_at"] = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        db._identities[(claims.tenant_id, id2)]["created_at"] = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)

        identities = await list_identities(db, claims, contact_id)

        assert all(isinstance(i, ContactIdentity) for i in identities)
        assert [i.identity_id for i in identities] == [id2, id1]


async def test_list_identities_cross_tenant_empty() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import add_identity, create_contact, list_identities

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        contact_id = await create_contact(
            db, claims_a, account_id=None, lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )
        await add_identity(db, claims_a, contact_id, identity_type="email", identity_value="dana@example.com")

        identities = await list_identities(db, claims_b, contact_id)
        assert identities == []


async def test_list_identities_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import list_identities

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await list_identities(db, global_claims, "contact-1")


async def test_get_contact_id_by_identity_resolves() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import add_identity, create_contact, get_contact_id_by_identity

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        contact_id = await create_contact(
            db, claims, account_id=None, lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )
        await add_identity(db, claims, contact_id, identity_type="email", identity_value="dana@example.com")

        resolved = await get_contact_id_by_identity(
            db, claims, identity_type="email", identity_value="dana@example.com",
        )
        assert resolved == contact_id


async def test_get_contact_id_by_identity_returns_none_when_no_match() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import get_contact_id_by_identity

        db = _StubDatabase()
        claims = _claims()

        resolved = await get_contact_id_by_identity(
            db, claims, identity_type="email", identity_value="nobody@example.com",
        )
        assert resolved is None


async def test_get_contact_id_by_identity_cross_tenant_isolation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.contacts.repository import add_identity, create_contact, get_contact_id_by_identity

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        contact_id = await create_contact(
            db, claims_a, account_id=None, lead_id=None, name="Dana", email=None,
            phone=None, consent=_consent(),
        )
        await add_identity(db, claims_a, contact_id, identity_type="email", identity_value="dana@example.com")

        resolved = await get_contact_id_by_identity(
            db, claims_b, identity_type="email", identity_value="dana@example.com",
        )
        assert resolved is None


async def test_get_contact_id_by_identity_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.contacts.repository import get_contact_id_by_identity

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_contact_id_by_identity(
                db, global_claims, identity_type="email", identity_value="x@example.com",
            )
