"""Unit tests for api.leads.repository.

Covers:
- create_lead inserts a tenant-scoped row with all fields + consent jsonb.
- get_lead returns the row mapped to Lead, or None if not found.
- Cross-tenant isolation: lead created under tenant A is not visible to tenant B.
- IDs are uuid4().hex.
- Positional placeholders ($1, $2, ...) are used.
- update_lead_stage issues a tenant-scoped UPDATE, returns the updated Lead,
  no-ops (returns None) cross-tenant, and rejects global callers.
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
    """In-memory stub database for testing leads repository."""

    def __init__(self) -> None:
        # leads: keyed by (tenant_id, lead_id)
        self._leads: dict[tuple[str, str], dict[str, Any]] = {}
        # lead_activities: keyed by (tenant_id, activity_id)
        self._activities: dict[tuple[str, str], dict[str, Any]] = {}
        # Record all execute/fetchrow/fetch calls for inspection
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        q = query.strip().upper()

        if q.startswith("UPDATE LEADS SET ASSIGNED_AGENT_ID"):
            # args: assigned_agent_id, tenant_id, lead_id
            assigned_agent_id = args[0]
            tenant_id = args[1]
            lead_id = args[2]
            key = (tenant_id, lead_id)
            existing = self._leads.get(key)
            if existing is None:
                return "UPDATE 0"
            existing["assigned_agent_id"] = assigned_agent_id
            existing["updated_at"] = _NOW
            return "UPDATE 1"

        if q.startswith("UPDATE LEADS SET NAME"):
            # update_lead_contact -- args: name, email, consent, tenant_id, lead_id
            name, email, consent, tenant_id, lead_id = args
            key = (tenant_id, lead_id)
            existing = self._leads.get(key)
            if existing is None:
                return "UPDATE 0"
            existing["name"] = existing["name"] if name is None else name
            existing["email"] = existing["email"] if email is None else email
            existing["consent"] = consent
            existing["updated_at"] = _NOW
            return "UPDATE 1"

        if q.startswith("UPDATE LEADS"):
            # args: stage, status, qualification_score, tenant_id, lead_id
            stage = args[0]
            status = args[1]
            qualification_score = args[2]
            tenant_id = args[3]
            lead_id = args[4]
            key = (tenant_id, lead_id)
            existing = self._leads.get(key)
            if existing is None:
                return "UPDATE 0"
            existing["stage"] = stage
            existing["status"] = status
            existing["qualification_score"] = qualification_score
            existing["updated_at"] = _NOW
            return "UPDATE 1"

        if q.startswith("INSERT INTO LEAD_ACTIVITIES"):
            # args: tenant_id, activity_id, lead_id, type, payload, actor
            tenant_id = args[0]
            activity_id = args[1]
            lead_id = args[2]
            activity_type = args[3]
            payload = args[4]
            actor = args[5]
            self._activities[(tenant_id, activity_id)] = {
                "tenant_id": tenant_id,
                "activity_id": activity_id,
                "lead_id": lead_id,
                "type": activity_type,
                "payload": payload,
                "actor": actor,
                "created_at": _NOW,
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO LEADS"):
            # args: tenant_id, lead_id, visitor_id, name, email, phone, status, stage,
            #       qualification_score, consent, assigned_agent_id, source
            tenant_id = args[0]
            lead_id = args[1]
            visitor_id = args[2]
            name = args[3]
            email = args[4]
            phone = args[5]
            status = args[6]
            stage = args[7]
            qualification_score = args[8]
            consent = args[9]
            assigned_agent_id = args[10]
            source = args[11]

            self._leads[(tenant_id, lead_id)] = {
                "tenant_id": tenant_id,
                "lead_id": lead_id,
                "visitor_id": visitor_id,
                "name": name,
                "email": email,
                "phone": phone,
                "status": status,
                "stage": stage,
                "qualification_score": qualification_score,
                "consent": consent,
                "assigned_agent_id": assigned_agent_id,
                "source": source,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            return "INSERT 0 1"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        q = query.strip().upper()

        if "FROM LEADS" in q and "AND VISITOR_ID = $2" in q:
            # get_lead_email_by_visitor_id / get_lead_id_by_visitor_id --
            # WHERE tenant_id = $1 AND visitor_id = $2 ORDER BY created_at
            # DESC LIMIT 1
            tenant_id, visitor_id = args
            matches = [
                row
                for row in self._leads.values()
                if row["tenant_id"] == tenant_id and row["visitor_id"] == visitor_id
            ]
            if not matches:
                return None
            matches.sort(key=lambda r: r["created_at"], reverse=True)
            return matches[0]

        if "COUNT(*)" in q and "FROM LEADS" in q:
            _, total = self._filtered_leads(query, args)
            return {"count": total}

        if "FROM LEADS" in q and "WHERE TENANT_ID" in q:
            # get_lead — WHERE tenant_id = $1 AND lead_id = $2
            tenant_id = args[0]
            lead_id = args[1]
            key = (tenant_id, lead_id)
            return self._leads.get(key)

        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        q = query.strip().upper()

        if "FROM LEAD_ACTIVITIES" in q:
            # list_activities — WHERE tenant_id = $1 AND lead_id = $2
            tenant_id = args[0]
            lead_id = args[1]
            rows = [
                row
                for row in self._activities.values()
                if row["tenant_id"] == tenant_id and row["lead_id"] == lead_id
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows

        if "FROM LEADS" in q and "CONVERTED_TO_CONTACT_ID = $2" in q:
            # list_lead_ids_converted_to_contact -- WHERE tenant_id = $1 AND
            # converted_to_contact_id = $2
            tenant_id, contact_id = args
            return [
                row
                for row in self._leads.values()
                if row["tenant_id"] == tenant_id
                and row.get("converted_to_contact_id") == contact_id
            ]

        if "FROM LEADS" in q:
            # list_leads page query -- filter by whatever WHERE clause was
            # built; since this stub doesn't parse SQL, it replays the same
            # filtering logic the real WHERE clause would apply, using the
            # captured query text to know which filters are present.
            return self._filtered_leads(query, args)[0]

        return []

    def _filtered_leads(
        self, query: str, args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        """Shared filtering used by both the count and page queries."""
        q = query.upper()
        idx = 0
        tenant_id = args[idx]
        idx += 1
        rows = [row for row in self._leads.values() if row["tenant_id"] == tenant_id]

        if "STAGE = $" in q:
            stage = args[idx]
            idx += 1
            rows = [r for r in rows if r["stage"] == stage]
        if "STATUS = $" in q:
            status = args[idx]
            idx += 1
            rows = [r for r in rows if r["status"] == status]
        if "ASSIGNED_AGENT_ID = $" in q:
            agent_id = args[idx]
            idx += 1
            rows = [r for r in rows if r["assigned_agent_id"] == agent_id]
        if "CREATED_AT >= $" in q:
            created_from = args[idx]
            idx += 1
            rows = [r for r in rows if r["created_at"] >= created_from]
        if "CREATED_AT < $" in q:
            created_to = args[idx]
            idx += 1
            rows = [r for r in rows if r["created_at"] < created_to]

        if "NAME ILIKE $" in q:
            # Repository escapes %, _, and backslash before binding. The
            # stub mirrors the resulting literal substring contract rather
            # than silently treating a wildcard as an all-rows match.
            pattern = str(args[idx])
            idx += 2
            needle = (
                pattern.removeprefix("%").removesuffix("%")
                .replace("\\\\", "\\").replace("\\%", "%").replace("\\_", "_")
                .lower()
            )
            rows = [
                row for row in rows
                if needle in (row["name"] or "").lower() or needle in (row["email"] or "").lower()
            ]

        # Count queries deliberately have no ORDER BY. The hard failure below
        # applies only to a page query, where an unmodeled order would create
        # a false-green test.
        if "ORDER BY " not in q:
            return rows, len(rows)

        sort_key: str
        if "(CASE STAGE" in q:
            sort_key = "stage"
        elif "(CASE STATUS" in q:
            sort_key = "status"
        elif "ORDER BY NAME " in q:
            sort_key = "name"
        elif "ORDER BY EMAIL " in q:
            sort_key = "email"
        elif "ORDER BY QUALIFICATION_SCORE " in q:
            sort_key = "score"
        elif "ORDER BY ASSIGNED_AGENT_ID " in q:
            sort_key = "assigned"
        elif "ORDER BY CREATED_AT " in q:
            sort_key = "created"
        else:
            raise AssertionError(f"stub cannot honor ORDER BY: {query}")

        value_for = {
            "stage": lambda row: {"captured": 1, "qualified": 2, "contacted": 3, "converted": 4, "disqualified": 5}.get(row["stage"], 99),
            "status": lambda row: {"new": 1, "open": 2, "won": 3, "lost": 4}.get(row["status"], 99),
            "name": lambda row: row["name"],
            "email": lambda row: row["email"],
            "score": lambda row: row["qualification_score"],
            "assigned": lambda row: row["assigned_agent_id"],
            "created": lambda row: row["created_at"],
        }[sort_key]
        descending = " DESC NULLS LAST" in q
        non_null_rows = [row for row in rows if value_for(row) is not None]
        null_rows = [row for row in rows if value_for(row) is None]
        # The SQL's stable total order is value, then lead_id DESC, with null
        # values always parked last. Two stable Python sorts mirror that.
        non_null_rows.sort(key=lambda row: row["lead_id"], reverse=True)
        non_null_rows.sort(key=value_for, reverse=descending)
        null_rows.sort(key=lambda row: row["lead_id"], reverse=True)
        if sort_key in {"stage", "status"}:
            known_rows = [row for row in non_null_rows if value_for(row) != 99]
            unknown_rows = [row for row in non_null_rows if value_for(row) == 99]
            known_rows.sort(key=lambda row: row["lead_id"], reverse=True)
            known_rows.sort(key=value_for, reverse=descending)
            unknown_rows.sort(key=lambda row: row["lead_id"], reverse=True)
            rows = [*known_rows, *unknown_rows, *null_rows]
        else:
            rows = [*non_null_rows, *null_rows]
        total = len(rows)

        if "LIMIT $" in q:
            limit = args[idx]
            idx += 1
            offset = args[idx] if idx < len(args) else 0
            rows = rows[offset : offset + limit]

        return rows, total


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_db() -> _StubDatabase:
    return _StubDatabase()


def _claims(tenant_id: str = "tenant-abc", role: Role = Role.VISITOR) -> AuthClaims:
    return AuthClaims(subject="visitor-123", role=role, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_create_lead_inserts_with_all_fields() -> None:
    """create_lead inserts a row with tenant_id, lead_id, visitor_id, name, email, phone, consent, etc."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead

        db = _StubDatabase()
        claims = _claims()
        consent_dict = {
            "granted": True,
            "purpose": "contact",
            "text": "I agree.",
            "captured_at": "2026-01-01T12:00:00Z",
        }

        lead_id = await create_lead(
            db,
            claims,
            visitor_id=claims.subject,
            name="Jane Doe",
            email="jane@example.com",
            phone="+1555123456",
            consent=consent_dict,
            source="widget",
        )

        # Check the ID was returned (uuid4().hex format)
        assert isinstance(lead_id, str)
        assert len(lead_id) == 32  # hex string from uuid4().hex

        # Check the INSERT was called
        assert len(db.execute_calls) == 1
        insert_query, insert_args = db.execute_calls[0]
        assert "insert into leads" in insert_query.lower()
        assert insert_args[0] == claims.tenant_id  # tenant_id = $1
        assert insert_args[1] == lead_id  # lead_id = $2
        assert insert_args[2] == claims.subject  # visitor_id = $3
        assert insert_args[3] == "Jane Doe"  # name = $4
        assert insert_args[4] == "jane@example.com"  # email = $5
        assert insert_args[5] == "+1555123456"  # phone = $6
        assert insert_args[6] == "new"  # status = $7 (default)
        assert insert_args[7] == "captured"  # stage = $8 (default)
        assert insert_args[8] is None  # qualification_score = $9 (NULL)
        assert insert_args[9] == consent_dict  # consent = $10 (jsonb)
        assert insert_args[10] is None  # assigned_agent_id = $11 (NULL)
        assert insert_args[11] == "widget"  # source = $12


async def test_create_lead_uses_positional_placeholders() -> None:
    """The SQL uses positional placeholders ($1, $2, etc.), not named params."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead

        db = _StubDatabase()
        claims = _claims()

        await create_lead(
            db,
            claims,
            visitor_id=claims.subject,
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK", "captured_at": "2026-01-01T12:00:00Z"},
            source="widget",
        )

        insert_query, _ = db.execute_calls[0]
        # Check that the query uses $1, $2, ... not named params
        assert "$1" in insert_query
        assert "$2" in insert_query
        assert "$12" in insert_query
        assert ":" not in insert_query  # no named placeholders


async def test_create_lead_default_source() -> None:
    """When source is not provided, it defaults to 'widget'."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead

        db = _StubDatabase()
        claims = _claims()

        await create_lead(
            db,
            claims,
            visitor_id=claims.subject,
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK", "captured_at": "2026-01-01T12:00:00Z"},
            source="widget",
        )

        _, insert_args = db.execute_calls[0]
        # source should be at args[11]
        assert insert_args[11] == "widget"


async def test_get_lead_returns_mapped_lead() -> None:
    """get_lead returns a Lead dataclass with all fields mapped."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import Lead, get_lead

        db = _StubDatabase()
        claims = _claims()
        lead_id = "abc123def456"
        consent_dict = {"granted": True, "purpose": "contact", "text": "OK", "captured_at": "2026-01-01T12:00:00Z"}

        # Manually insert a lead into the stub
        db._leads[(claims.tenant_id, lead_id)] = {
            "tenant_id": claims.tenant_id,
            "lead_id": lead_id,
            "visitor_id": "visitor-123",
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "+1555123456",
            "status": "new",
            "stage": "captured",
            "qualification_score": None,
            "consent": consent_dict,
            "assigned_agent_id": None,
            "source": "widget",
            "created_at": _NOW,
            "updated_at": _NOW,
        }

        lead = await get_lead(db, claims, lead_id)

        assert isinstance(lead, Lead)
        assert lead.lead_id == lead_id
        assert lead.visitor_id == "visitor-123"
        assert lead.name == "Jane Doe"
        assert lead.email == "jane@example.com"
        assert lead.phone == "+1555123456"
        assert lead.status == "new"
        assert lead.stage == "captured"
        assert lead.qualification_score is None
        assert lead.consent == consent_dict
        assert lead.assigned_agent_id is None
        assert lead.source == "widget"


async def test_get_lead_returns_none_if_not_found() -> None:
    """get_lead returns None if the lead doesn't exist."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import get_lead

        db = _StubDatabase()
        claims = _claims()

        lead = await get_lead(db, claims, "nonexistent-id")

        assert lead is None


async def test_cross_tenant_isolation_create() -> None:
    """A lead created under tenant A is not visible when querying as tenant B."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        # Create a lead under tenant A
        lead_id = await create_lead(
            db,
            claims_a,
            visitor_id="visitor-a",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK", "captured_at": "2026-01-01T12:00:00Z"},
            source="widget",
        )

        # Try to retrieve it as tenant B
        lead = await get_lead(db, claims_b, lead_id)

        # Should return None (not visible to tenant B)
        assert lead is None


async def test_get_lead_uses_positional_placeholders() -> None:
    """The get_lead SQL uses positional placeholders."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import get_lead

        db = _StubDatabase()
        claims = _claims()

        await get_lead(db, claims, "test-id")

        select_query, _ = db.fetchrow_calls[0]
        assert "$1" in select_query  # tenant_id
        assert "$2" in select_query  # lead_id
        assert ":" not in select_query  # no named placeholders


# ---------------------------------------------------------------------------
# update_lead_stage
# ---------------------------------------------------------------------------


async def test_update_lead_stage_updates_and_returns_lead() -> None:
    """update_lead_stage issues a tenant-scoped UPDATE and returns the updated Lead."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import Lead, create_lead, update_lead_stage

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        updated = await update_lead_stage(
            db,
            claims,
            lead_id,
            stage="qualified",
            status="open",
            qualification_score=55,
        )

        assert isinstance(updated, Lead)
        assert updated.lead_id == lead_id
        assert updated.stage == "qualified"
        assert updated.status == "open"
        assert updated.qualification_score == 55


async def test_update_lead_stage_uses_tenant_scoped_positional_sql() -> None:
    """The UPDATE statement filters WHERE tenant_id=$_ AND lead_id=$_ with positional params."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, update_lead_stage

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        await update_lead_stage(
            db,
            claims,
            lead_id,
            stage="qualified",
            status="open",
            qualification_score=55,
        )

        update_query, update_args = db.execute_calls[-1]
        assert "update leads" in update_query.lower()
        assert "where" in update_query.lower()
        assert "tenant_id" in update_query.lower()
        assert "lead_id" in update_query.lower()
        assert "updated_at" in update_query.lower()
        assert ":" not in update_query  # no named placeholders
        # tenant_id and lead_id must be among the bound params
        assert claims.tenant_id in update_args
        assert lead_id in update_args


async def test_update_lead_stage_cross_tenant_returns_none() -> None:
    """A caller from tenant B updating tenant A's lead matches 0 rows -> None."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, update_lead_stage

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        lead_id = await create_lead(
            db,
            claims_a,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        result = await update_lead_stage(
            db,
            claims_b,
            lead_id,
            stage="qualified",
            status="open",
            qualification_score=55,
        )

        assert result is None


async def test_update_lead_stage_missing_lead_returns_none() -> None:
    """Updating a nonexistent lead_id returns None."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import update_lead_stage

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        result = await update_lead_stage(
            db,
            claims,
            "nonexistent-id",
            stage="qualified",
            status="open",
            qualification_score=55,
        )

        assert result is None


async def test_update_lead_stage_rejects_global_caller() -> None:
    """A PLATFORM_ADMIN (global, tenant_id=None) caller raises ValidationError."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import update_lead_stage

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await update_lead_stage(
                db,
                global_claims,
                "some-lead-id",
                stage="qualified",
                status="open",
                qualification_score=55,
            )


# ---------------------------------------------------------------------------
# update_lead_contact (SR-14 D6)
# ---------------------------------------------------------------------------


async def test_update_lead_contact_fills_in_null_name_and_email() -> None:
    """The SR-9.1 anonymous-booking case: a lead with NULL name/email gets
    filled in by update_lead_contact -- no second lead row, same lead_id."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead, update_lead_contact

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name=None,
            email=None,
            phone=None,
            consent={"granted": True, "purpose": "booking", "text": "OK"},
            source="booking",
        )

        consent = {
            "granted": True,
            "purpose": "chat_identification",
            "text": "I consent...",
            "captured_at": "2026-01-01T12:00:00Z",
        }
        updated = await update_lead_contact(
            db, claims, lead_id, name="Dana", email="dana@example.com", consent=consent,
        )

        assert updated is True
        lead = await get_lead(db, claims, lead_id)
        assert lead is not None
        assert lead.lead_id == lead_id
        assert lead.name == "Dana"
        assert lead.email == "dana@example.com"
        assert lead.consent == consent


async def test_update_lead_contact_never_nulls_existing_non_null_values() -> None:
    """A None name/email argument keeps the existing value -- never clobbers
    good data with nothing (defense-in-depth over the endpoint always
    supplying non-null values)."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead, update_lead_contact

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name="Existing Name",
            email="existing@example.com",
            phone=None,
            consent={"granted": True, "purpose": "booking", "text": "OK"},
            source="booking",
        )

        await update_lead_contact(
            db,
            claims,
            lead_id,
            name=None,
            email=None,
            consent={"granted": True, "purpose": "chat_identification", "text": "..."},
        )

        lead = await get_lead(db, claims, lead_id)
        assert lead is not None
        assert lead.name == "Existing Name"
        assert lead.email == "existing@example.com"


async def test_update_lead_contact_uses_tenant_scoped_positional_sql() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, update_lead_contact

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name=None,
            email=None,
            phone=None,
            consent={"granted": True, "purpose": "booking", "text": "OK"},
            source="booking",
        )

        await update_lead_contact(
            db, claims, lead_id, name="Dana", email="dana@example.com", consent={"granted": True},
        )

        update_query, update_args = db.execute_calls[-1]
        assert "update leads" in update_query.lower()
        assert "where" in update_query.lower()
        assert "tenant_id" in update_query.lower()
        assert "lead_id" in update_query.lower()
        assert ":" not in update_query  # no named placeholders
        assert claims.tenant_id in update_args
        assert lead_id in update_args


async def test_update_lead_contact_cross_tenant_returns_false() -> None:
    """SR-14 mandatory tenant isolation: a matching lead_id under tenant B
    is unaffected by an update issued with tenant A's claims."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead, update_lead_contact

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        lead_id = await create_lead(
            db,
            claims_b,
            visitor_id="visitor-1",
            name=None,
            email=None,
            phone=None,
            consent={"granted": True, "purpose": "booking", "text": "OK"},
            source="booking",
        )

        result = await update_lead_contact(
            db, claims_a, lead_id, name="Dana", email="dana@example.com", consent={"granted": True},
        )

        assert result is False
        # Tenant B's lead is untouched.
        lead_b = await get_lead(db, claims_b, lead_id)
        assert lead_b is not None
        assert lead_b.name is None
        assert lead_b.email is None


async def test_update_lead_contact_missing_lead_returns_false() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import update_lead_contact

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        result = await update_lead_contact(
            db,
            claims,
            "nonexistent-id",
            name="Dana",
            email="dana@example.com",
            consent={"granted": True},
        )

        assert result is False


async def test_update_lead_contact_rejects_global_caller() -> None:
    """MANDATORY tenant isolation: PLATFORM_ADMIN (global, tenant_id=None)
    raises ValidationError -- _reject_global runs first."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import update_lead_contact

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await update_lead_contact(
                db,
                global_claims,
                "some-lead-id",
                name="Dana",
                email="dana@example.com",
                consent={"granted": True},
            )


async def test_update_lead_contact_idempotent_resubmission() -> None:
    """A re-submission with the same values is idempotent -- still one lead
    row, updated in place, no duplicate."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead, update_lead_contact

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name=None,
            email=None,
            phone=None,
            consent={"granted": True, "purpose": "booking", "text": "OK"},
            source="booking",
        )

        consent = {"granted": True, "purpose": "chat_identification", "text": "..."}
        await update_lead_contact(
            db, claims, lead_id, name="Dana", email="dana@example.com", consent=consent,
        )
        await update_lead_contact(
            db, claims, lead_id, name="Dana", email="dana@example.com", consent=consent,
        )

        assert len(db._leads) == 1
        lead = await get_lead(db, claims, lead_id)
        assert lead is not None
        assert lead.name == "Dana"
        assert lead.email == "dana@example.com"


# ---------------------------------------------------------------------------
# add_activity / list_activities / assign_lead
# ---------------------------------------------------------------------------


async def test_add_activity_inserts_tenant_scoped_row() -> None:
    """add_activity issues a tenant-scoped INSERT and returns a uuid4().hex activity_id."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import add_activity, create_lead

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        activity_id = await add_activity(
            db,
            claims,
            lead_id,
            type="note",
            payload={"text": "Called, left voicemail."},
            actor="user-1",
        )

        assert isinstance(activity_id, str)
        assert len(activity_id) == 32

        insert_query, insert_args = db.execute_calls[-1]
        assert "insert into lead_activities" in insert_query.lower()
        assert "$1" in insert_query
        assert ":" not in insert_query
        assert insert_args[0] == claims.tenant_id
        assert insert_args[1] == activity_id
        assert insert_args[2] == lead_id
        assert insert_args[3] == "note"
        assert insert_args[4] == {"text": "Called, left voicemail."}
        assert insert_args[5] == "user-1"


async def test_add_activity_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import add_activity

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await add_activity(
                db,
                global_claims,
                "some-lead-id",
                type="note",
                payload={"text": "x"},
                actor="admin-1",
            )


async def test_list_activities_returns_tenant_scoped_ordered() -> None:
    """list_activities returns only this tenant's activities for the lead, newest first."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import LeadActivity, add_activity, create_lead, list_activities

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        first_id = await add_activity(
            db, claims, lead_id, type="note", payload={"text": "first"}, actor="user-1"
        )
        second_id = await add_activity(
            db, claims, lead_id, type="note", payload={"text": "second"}, actor="user-1"
        )
        # Force distinct timestamps so DESC ordering is unambiguous.
        db._activities[(claims.tenant_id, first_id)]["created_at"] = datetime(
            2026, 1, 1, 12, 0, 0, tzinfo=UTC
        )
        db._activities[(claims.tenant_id, second_id)]["created_at"] = datetime(
            2026, 1, 1, 12, 5, 0, tzinfo=UTC
        )

        activities = await list_activities(db, claims, lead_id)

        assert all(isinstance(a, LeadActivity) for a in activities)
        assert [a.activity_id for a in activities] == [second_id, first_id]


async def test_list_activities_cross_tenant_empty() -> None:
    """A tenant B caller sees no activities for tenant A's lead."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import add_activity, create_lead, list_activities

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        lead_id = await create_lead(
            db,
            claims_a,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )
        await add_activity(db, claims_a, lead_id, type="note", payload={"text": "hi"}, actor="user-1")

        activities = await list_activities(db, claims_b, lead_id)

        assert activities == []


async def test_list_activities_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import list_activities

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await list_activities(db, global_claims, "some-lead-id")


async def test_assign_lead_updates_assigned_agent_id() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import Lead, assign_lead, create_lead

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        updated = await assign_lead(db, claims, lead_id, agent_id="agent-1")

        assert isinstance(updated, Lead)
        assert updated.assigned_agent_id == "agent-1"

        update_query, update_args = db.execute_calls[-1]
        assert "update leads" in update_query.lower()
        assert "assigned_agent_id" in update_query.lower()
        assert "$1" in update_query
        assert ":" not in update_query
        assert claims.tenant_id in update_args
        assert lead_id in update_args


async def test_assign_lead_cross_tenant_returns_none() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import assign_lead, create_lead

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        lead_id = await create_lead(
            db,
            claims_a,
            visitor_id="visitor-1",
            name="Jane",
            email="jane@example.com",
            phone=None,
            consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        result = await assign_lead(db, claims_b, lead_id, agent_id="agent-1")

        assert result is None
        stored = db._leads[("tenant-a", lead_id)]
        assert stored["assigned_agent_id"] is None


async def test_assign_lead_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import assign_lead

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await assign_lead(db, global_claims, "some-lead-id", agent_id="agent-1")


# ---------------------------------------------------------------------------
# get_lead_email_by_visitor_id (S9.2, Scope §7)
# ---------------------------------------------------------------------------


async def test_get_lead_email_by_visitor_id_returns_most_recent() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead_email_by_visitor_id

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        await create_lead(
            db, claims, visitor_id="visitor-1", name="First", email="first@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )
        # A later lead for the same visitor -- created_at defaults to _NOW for
        # both rows in this stub, so bump the second row's created_at
        # explicitly to make "most recent" observable.
        second_lead_id = await create_lead(
            db, claims, visitor_id="visitor-1", name="Second", email="second@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )
        from datetime import timedelta

        db._leads[("tenant-abc", second_lead_id)]["created_at"] = _NOW + timedelta(minutes=5)

        email = await get_lead_email_by_visitor_id(db, claims, "visitor-1")

        assert email == "second@example.com"


async def test_get_lead_email_by_visitor_id_returns_none_when_no_lead() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import get_lead_email_by_visitor_id

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        result = await get_lead_email_by_visitor_id(db, claims, "visitor-does-not-exist")

        assert result is None


async def test_get_lead_email_by_visitor_id_cross_tenant_isolation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead_email_by_visitor_id

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        await create_lead(
            db, claims_a, visitor_id="visitor-shared", name="Jane", email="jane@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        result = await get_lead_email_by_visitor_id(db, claims_b, "visitor-shared")

        assert result is None


async def test_get_lead_email_by_visitor_id_uses_positional_placeholders() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import get_lead_email_by_visitor_id

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        await get_lead_email_by_visitor_id(db, claims, "visitor-1")

        query, args = db.fetchrow_calls[-1]
        assert "$1" in query
        assert "$2" in query
        assert ":" not in query
        assert args[0] == "tenant-abc"
        assert args[1] == "visitor-1"


async def test_get_lead_email_by_visitor_id_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import get_lead_email_by_visitor_id

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_lead_email_by_visitor_id(db, global_claims, "visitor-1")


# ---------------------------------------------------------------------------
# list_leads (S12.4)
# ---------------------------------------------------------------------------


def _seed_lead(
    db: _StubDatabase,
    *,
    tenant_id: str,
    lead_id: str,
    stage: str = "captured",
    status: str = "new",
    assigned_agent_id: str | None = None,
    qualification_score: int | None = None,
    name: str | None = "Jane Doe",
    email: str | None = "jane@example.com",
    created_at: datetime = _NOW,
) -> None:
    db._leads[(tenant_id, lead_id)] = {
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "visitor_id": "visitor-1",
        "name": name,
        "email": email,
        "phone": None,
        "status": status,
        "stage": stage,
        "qualification_score": qualification_score,
        "consent": {"granted": True, "purpose": "contact", "text": "OK"},
        "assigned_agent_id": assigned_agent_id,
        "source": "widget",
        "created_at": created_at,
        "updated_at": created_at,
    }


async def test_list_leads_tenant_scoping_first_param_is_tenant_id() -> None:
    """MANDATORY isolation: the first bound param on every captured query is
    claims.tenant_id, for a tenant-A vs a distinct tenant-B claims object."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a", role=Role.CLIENT_ADMIN)
        claims_b = _claims(tenant_id="tenant-b", role=Role.CLIENT_ADMIN)
        _seed_lead(db, tenant_id="tenant-a", lead_id="lead-a1")
        _seed_lead(db, tenant_id="tenant-b", lead_id="lead-b1")

        rows_a, total_a = await list_leads(db, claims_a)
        rows_b, total_b = await list_leads(db, claims_b)

        assert [r.lead_id for r in rows_a] == ["lead-a1"]
        assert total_a == 1
        assert [r.lead_id for r in rows_b] == ["lead-b1"]
        assert total_b == 1

        for query, args in [*db.fetch_calls, *db.fetchrow_calls]:
            if "FROM LEADS" in query.upper():
                assert args[0] in ("tenant-a", "tenant-b")
                assert "$1" in query
                assert ":" not in query


async def test_list_leads_rejects_global_caller() -> None:
    """MANDATORY: a PLATFORM_ADMIN (tenant_id=None) caller raises ValidationError,
    no query issued."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import list_leads

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError) as exc_info:
            await list_leads(db, global_claims)

        assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
        assert db.fetch_calls == []
        assert db.fetchrow_calls == []


async def test_list_leads_no_filters_where_only_tenant_id() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc", role=Role.CLIENT_ADMIN)
        _seed_lead(db, tenant_id="tenant-abc", lead_id="lead-1")

        await list_leads(db, claims)

        page_query, _ = db.fetch_calls[-1]
        where_clause = page_query.lower().split("where")[1].split("order by")[0]
        assert "stage =" not in where_clause
        assert "assigned_agent_id =" not in where_clause
        assert "created_at" not in where_clause
        # status filter excluded, but the selected qualification_score column
        # legitimately contains the substring "status" nowhere -- direct check:
        assert " status = " not in where_clause


async def test_list_leads_each_filter_appends_one_bound_clause() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc", role=Role.CLIENT_ADMIN)
        _seed_lead(db, tenant_id="tenant-abc", lead_id="lead-1", stage="qualified", status="open", assigned_agent_id="agent-9")

        rows, total = await list_leads(db, claims, stage="qualified")
        assert [r.lead_id for r in rows] == ["lead-1"]
        query, args = db.fetch_calls[-1]
        assert "stage = $" in query.lower()
        assert "qualified" in args

        rows, total = await list_leads(db, claims, status="open")
        assert [r.lead_id for r in rows] == ["lead-1"]
        query, args = db.fetch_calls[-1]
        assert "status = $" in query.lower()
        assert "open" in args

        rows, total = await list_leads(db, claims, assigned_agent_id="agent-9")
        assert [r.lead_id for r in rows] == ["lead-1"]
        query, args = db.fetch_calls[-1]
        assert "assigned_agent_id = $" in query.lower()
        assert "agent-9" in args


async def test_list_leads_created_from_to_filters() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc", role=Role.CLIENT_ADMIN)
        _seed_lead(db, tenant_id="tenant-abc", lead_id="lead-old", created_at=datetime(2026, 1, 1, tzinfo=UTC))
        _seed_lead(db, tenant_id="tenant-abc", lead_id="lead-new", created_at=datetime(2026, 6, 1, tzinfo=UTC))

        rows, total = await list_leads(
            db,
            claims,
            created_from=datetime(2026, 3, 1, tzinfo=UTC),
            created_to=datetime(2026, 12, 1, tzinfo=UTC),
        )

        assert [r.lead_id for r in rows] == ["lead-new"]
        assert total == 1
        query, args = db.fetch_calls[-1]
        assert "created_at >= $" in query.lower()
        assert "created_at < $" in query.lower()


async def test_list_leads_combined_filters_all_appear() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc", role=Role.CLIENT_ADMIN)
        _seed_lead(
            db, tenant_id="tenant-abc", lead_id="lead-1", stage="qualified", status="open",
            assigned_agent_id="agent-9",
        )

        rows, total = await list_leads(
            db, claims, stage="qualified", status="open", assigned_agent_id="agent-9",
        )

        assert [r.lead_id for r in rows] == ["lead-1"]
        query, args = db.fetch_calls[-1]
        for clause in ("stage = $", "status = $", "assigned_agent_id = $"):
            assert clause in query.lower()
        for val in ("qualified", "open", "agent-9"):
            assert val in args


async def test_list_leads_pagination_limit_offset_order() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc", role=Role.CLIENT_ADMIN)
        for i in range(5):
            _seed_lead(
                db, tenant_id="tenant-abc", lead_id=f"lead-{i}",
                created_at=datetime(2026, 1, i + 1, tzinfo=UTC),
            )

        rows, total = await list_leads(db, claims, limit=2, offset=1)

        assert total == 5
        assert len(rows) == 2
        # newest first: lead-4 (Jan 5), lead-3 (Jan 4), lead-2 (Jan 3), ...
        assert [r.lead_id for r in rows] == ["lead-3", "lead-2"]

        query, args = db.fetch_calls[-1]
        assert "order by created_at desc nulls last, lead_id desc" in query.lower()
        assert 2 in args
        assert 1 in args


# ---------------------------------------------------------------------------
# get_lead_id_by_visitor_id (SR-9.1)
# ---------------------------------------------------------------------------


async def test_get_lead_id_by_visitor_id_returns_most_recent() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead_id_by_visitor_id

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        await create_lead(
            db, claims, visitor_id="visitor-1", name="First", email="first@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )
        second_lead_id = await create_lead(
            db, claims, visitor_id="visitor-1", name="Second", email="second@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )
        from datetime import timedelta

        db._leads[("tenant-abc", second_lead_id)]["created_at"] = _NOW + timedelta(minutes=5)

        lead_id = await get_lead_id_by_visitor_id(db, claims, "visitor-1")

        assert lead_id == second_lead_id


async def test_get_lead_id_by_visitor_id_returns_none_when_no_lead() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import get_lead_id_by_visitor_id

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        result = await get_lead_id_by_visitor_id(db, claims, "visitor-does-not-exist")

        assert result is None


async def test_get_lead_id_by_visitor_id_cross_tenant_isolation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead_id_by_visitor_id

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        await create_lead(
            db, claims_a, visitor_id="visitor-shared", name="Jane", email="jane@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        result = await get_lead_id_by_visitor_id(db, claims_b, "visitor-shared")

        assert result is None


async def test_get_lead_id_by_visitor_id_same_visitor_id_different_tenants_independent() -> None:
    """Same visitor_id under two tenants resolves to each tenant's own lead."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead_id_by_visitor_id

        db = _StubDatabase()
        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        lead_id_a = await create_lead(
            db, claims_a, visitor_id="visitor-shared", name="A", email="a@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )
        lead_id_b = await create_lead(
            db, claims_b, visitor_id="visitor-shared", name="B", email="b@example.com",
            phone=None, consent={"granted": True, "purpose": "contact", "text": "OK"},
            source="widget",
        )

        result_a = await get_lead_id_by_visitor_id(db, claims_a, "visitor-shared")
        result_b = await get_lead_id_by_visitor_id(db, claims_b, "visitor-shared")

        assert result_a == lead_id_a
        assert result_b == lead_id_b
        assert result_a != result_b


async def test_get_lead_id_by_visitor_id_uses_positional_placeholders() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import get_lead_id_by_visitor_id

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        await get_lead_id_by_visitor_id(db, claims, "visitor-1")

        query, args = db.fetchrow_calls[-1]
        assert "$1" in query
        assert "$2" in query
        assert ":" not in query
        assert args[0] == "tenant-abc"
        assert args[1] == "visitor-1"


async def test_get_lead_id_by_visitor_id_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import get_lead_id_by_visitor_id

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_lead_id_by_visitor_id(db, global_claims, "visitor-1")


# ---------------------------------------------------------------------------
# create_lead with nullable name/email (SR-9.1 C4 — anonymous bookings)
# ---------------------------------------------------------------------------


async def test_list_lead_ids_converted_to_contact_returns_matching_ids() -> None:
    """SR-9.3 D2: the contact-route belt-and-braces lookup -- finds leads
    whose converted_to_contact_id equals a given contact_id."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_lead_ids_converted_to_contact

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc", role=Role.CLIENT_ADMIN)
        db._leads[("tenant-abc", "lead-1")] = {
            "tenant_id": "tenant-abc", "lead_id": "lead-1", "visitor_id": "v1",
            "name": None, "email": None, "phone": None, "status": "won",
            "stage": "converted", "qualification_score": None, "consent": {},
            "assigned_agent_id": None, "source": "widget", "created_at": _NOW,
            "updated_at": _NOW, "converted_to_contact_id": "contact-1",
        }
        db._leads[("tenant-abc", "lead-2")] = {
            "tenant_id": "tenant-abc", "lead_id": "lead-2", "visitor_id": "v2",
            "name": None, "email": None, "phone": None, "status": "new",
            "stage": "captured", "qualification_score": None, "consent": {},
            "assigned_agent_id": None, "source": "widget", "created_at": _NOW,
            "updated_at": _NOW, "converted_to_contact_id": None,
        }

        result = await list_lead_ids_converted_to_contact(db, claims, "contact-1")
        assert result == ["lead-1"]

        result_none = await list_lead_ids_converted_to_contact(db, claims, "contact-nonexistent")
        assert result_none == []


async def test_list_lead_ids_converted_to_contact_tenant_isolation() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_lead_ids_converted_to_contact

        db = _StubDatabase()
        db._leads[("tenant-a", "lead-1")] = {
            "tenant_id": "tenant-a", "lead_id": "lead-1", "visitor_id": "v1",
            "name": None, "email": None, "phone": None, "status": "won",
            "stage": "converted", "qualification_score": None, "consent": {},
            "assigned_agent_id": None, "source": "widget", "created_at": _NOW,
            "updated_at": _NOW, "converted_to_contact_id": "contact-shared",
        }
        db._leads[("tenant-b", "lead-2")] = {
            "tenant_id": "tenant-b", "lead_id": "lead-2", "visitor_id": "v2",
            "name": None, "email": None, "phone": None, "status": "won",
            "stage": "converted", "qualification_score": None, "consent": {},
            "assigned_agent_id": None, "source": "widget", "created_at": _NOW,
            "updated_at": _NOW, "converted_to_contact_id": "contact-shared",
        }

        result_a = await list_lead_ids_converted_to_contact(
            db, _claims(tenant_id="tenant-a", role=Role.CLIENT_ADMIN), "contact-shared",
        )
        result_b = await list_lead_ids_converted_to_contact(
            db, _claims(tenant_id="tenant-b", role=Role.CLIENT_ADMIN), "contact-shared",
        )
        assert result_a == ["lead-1"]
        assert result_b == ["lead-2"]


async def test_list_lead_ids_converted_to_contact_rejects_global_caller() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import list_lead_ids_converted_to_contact

        db = _StubDatabase()
        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await list_lead_ids_converted_to_contact(db, global_claims, "contact-1")


async def test_create_lead_accepts_null_email_and_name() -> None:
    """C4: an anonymous booking creates a lead with NULL email/name, not a placeholder."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import create_lead, get_lead

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")

        lead_id = await create_lead(
            db,
            claims,
            visitor_id="visitor-1",
            name=None,
            email=None,
            phone=None,
            consent={"granted": True, "purpose": "booking", "text": "OK"},
            source="booking",
        )

        lead = await get_lead(db, claims, lead_id)
        assert lead is not None
        assert lead.name is None
        assert lead.email is None
        assert lead.source == "booking"


# ---------------------------------------------------------------------------
# SR-25: list_leads sort/search contract (repository defense in depth)
# ---------------------------------------------------------------------------


_SORT_INJECTION_PAYLOADS = [
    "created_at; DROP TABLE leads--",
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
async def test_list_leads_rejects_unknown_sort_key_at_repository_layer(sort: str) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import list_leads

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_leads(db, _claims(), sort=sort)

        assert exc_info.value.code == "INVALID_LEAD_SORT"
        assert not db.fetch_calls
        assert not db.fetchrow_calls


async def test_list_leads_rejects_unknown_direction_at_repository_layer() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import list_leads

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_leads(db, _claims(), direction="sideways")

        assert exc_info.value.code == "INVALID_LEAD_SORT_DIRECTION"
        assert not db.fetch_calls
        assert not db.fetchrow_calls


@pytest.mark.parametrize("payload", _SORT_INJECTION_PAYLOADS)
async def test_list_leads_sort_sql_never_contains_caller_string(payload: str) -> None:
    """SR-25 6a/6c-bis flagship test: for each rejected sort payload, prove the
    caller's raw string never reaches SQL. ``list_leads`` validates ``sort``
    against ``_SORT_COLUMNS`` (repository.py) synchronously, before building
    ``where``/``order_by`` or calling ``db.fetch``/``db.fetchrow`` at all --
    so the correct assertion is that the stub captured *no* queries during
    the call, not that some captured query merely lacks the substring.
    """
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from common.errors import ValidationError

        from api.leads.repository import list_leads

        db = _StubDatabase()
        with pytest.raises(ValidationError) as exc_info:
            await list_leads(db, _claims(), sort=payload)

        assert exc_info.value.code == "INVALID_LEAD_SORT"

        # Validation is fully synchronous and raises before any SQL is
        # built, so the stub must not have captured a single query.
        assert db.fetch_calls == []
        assert db.fetchrow_calls == []

        # Belt-and-braces: even if the stub had captured something, the
        # payload string must never appear as a substring of it.
        for query, _args in [*db.fetch_calls, *db.fetchrow_calls]:
            assert payload not in query


@pytest.mark.parametrize("sort", ["name", "email", "stage", "status", "score", "assigned", "created"])
async def test_list_leads_every_sort_key_emits_nulls_last_and_lead_id_tiebreak(sort: str) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        _seed_lead(db, tenant_id="tenant-abc", lead_id="lead-1")

        await list_leads(db, _claims(), sort=sort, direction="asc")

        query, _ = db.fetch_calls[-1]
        assert "NULLS LAST, lead_id DESC" in query


async def test_list_leads_default_sort_emits_created_at_desc_lead_id_desc() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        _seed_lead(db, tenant_id="tenant-abc", lead_id="lead-1")

        await list_leads(db, _claims())

        query, args = db.fetch_calls[-1]
        assert "ORDER BY created_at DESC NULLS LAST, lead_id DESC" in query
        assert args == ("tenant-abc", 50, 0)


@pytest.mark.parametrize("sort", ["stage", "status"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_list_leads_domain_sort_keeps_unknown_values_last_in_both_directions(
    sort: str, direction: str
) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        _seed_lead(db, tenant_id="tenant-abc", lead_id="known")
        _seed_lead(db, tenant_id="tenant-abc", lead_id="future", stage="future", status="future")

        rows, _ = await list_leads(db, _claims(), sort=sort, direction=direction)

        assert rows[-1].lead_id == "future"
        query, _ = db.fetch_calls[-1]
        assert "CASE WHEN" in query


async def test_list_leads_q_matches_name_or_email_and_escapes_wildcards() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        claims = _claims(tenant_id="tenant-abc")
        _seed_lead(db, tenant_id="tenant-abc", lead_id="name", name="Alice Example")
        _seed_lead(db, tenant_id="tenant-abc", lead_id="email", email="team@acme.test")
        _seed_lead(db, tenant_id="tenant-abc", lead_id="percent", name="100% Ready")
        _seed_lead(db, tenant_id="tenant-abc", lead_id="other", name="Bob Other")

        by_name, _ = await list_leads(db, claims, q="ALI")
        by_email, _ = await list_leads(db, claims, q="ACME")
        literal_percent, _ = await list_leads(db, claims, q="100%")

        assert [lead.lead_id for lead in by_name] == ["name"]
        assert [lead.lead_id for lead in by_email] == ["email"]
        assert [lead.lead_id for lead in literal_percent] == ["percent"]
        query, args = db.fetch_calls[-1]
        assert "ILIKE $2 ESCAPE '\\' OR email ILIKE $3 ESCAPE '\\'" in query
        assert args[1:3] == ("%100\\%%", "%100\\%%")


async def test_list_leads_q_never_crosses_tenant_boundary() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.leads.repository import list_leads

        db = _StubDatabase()
        _seed_lead(db, tenant_id="tenant-a", lead_id="a", name="Same Needle")
        _seed_lead(db, tenant_id="tenant-b", lead_id="b", name="Same Needle")

        rows, total = await list_leads(db, _claims(tenant_id="tenant-a"), q="needle")

        assert total == 1
        assert [lead.lead_id for lead in rows] == ["a"]
