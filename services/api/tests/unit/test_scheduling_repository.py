"""Unit tests for api.scheduling.repository.

Covers:
- upsert_availability / get_availability: tenant-scoped, rules+tz as jsonb/params.
- list_booked: tenant-scoped + windowed.
- create_event: tenant-scoped INSERT, uuid4().hex event_id.
- A simulated unique-violation (asyncpg.UniqueViolationError) on create_event ->
  ValidationError code SLOT_UNAVAILABLE.
- Cross-tenant isolation.
- Global caller (PLATFORM_ADMIN) -> ValidationError for every method.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import asyncpg
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

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

_RULES = {
    "slot_minutes": 30,
    "buffer_minutes": 0,
    "weekly_hours": {"mon": [["09:00", "17:00"]], "tue": [], "wed": [], "thu": [],
                      "fri": [], "sat": [], "sun": []},
}


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims(tenant_id: str = "tenant-abc", role: Role = Role.VISITOR) -> AuthClaims:
    return AuthClaims(subject="visitor-123", role=role, tenant_id=tenant_id)


class _StubDatabase:
    """In-memory stub database for testing the scheduling repository."""

    def __init__(self) -> None:
        self._availability: dict[str, dict[str, Any]] = {}
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        # When set, the next INSERT INTO schedule_events raises this exception.
        self.raise_on_insert_event: Exception | None = None

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        q = query.strip().upper()

        if q.startswith("INSERT INTO AVAILABILITY"):
            # args: tenant_id, timezone, rules
            tenant_id, timezone, rules = args
            self._availability[tenant_id] = {
                "tenant_id": tenant_id,
                "timezone": timezone,
                "rules": rules,
                "updated_at": _NOW,
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO SCHEDULE_EVENTS"):
            if self.raise_on_insert_event is not None:
                exc = self.raise_on_insert_event
                self.raise_on_insert_event = None
                raise exc
            # args: tenant_id, event_id, lead_id, visitor_id, email, name, starts_at,
            #       ends_at, timezone, status, calendar_ref, consent, source
            (tenant_id, event_id, lead_id, visitor_id, email, name, starts_at, ends_at,
             timezone, status, calendar_ref, consent, source) = args
            key = (tenant_id, event_id)
            for (t_id, _e_id), existing in self._events.items():
                if (
                    t_id == tenant_id
                    and existing["starts_at"] == starts_at
                    and existing["status"] == "booked"
                    and status == "booked"
                    and source == "native"
                    and existing.get("source", "native") == "native"
                ):
                    raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
            self._events[key] = {
                "tenant_id": tenant_id,
                "event_id": event_id,
                "lead_id": lead_id,
                "visitor_id": visitor_id,
                "email": email,
                "name": name,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "timezone": timezone,
                "status": status,
                "calendar_ref": calendar_ref,
                "consent": consent,
                "created_at": _NOW,
                "source": source,
                "meet_url": None,
            }
            return "INSERT 0 1"

        if q.startswith("UPDATE SCHEDULE_EVENTS SET STATUS = 'CANCELLED'"):
            tenant_id, calendar_ref = args
            for (t_id, _e_id), existing in self._events.items():
                if (
                    t_id == tenant_id
                    and existing["calendar_ref"] == calendar_ref
                    and existing.get("source") == "calendly"
                ):
                    existing["status"] = "cancelled"
            return "UPDATE 1"

        if q.startswith("UPDATE SCHEDULE_EVENTS SET LEAD_ID"):
            # set_event_lead_id -- args: lead_id, tenant_id, event_id
            lead_id, tenant_id, event_id = args
            key = (tenant_id, event_id)
            if key not in self._events:
                return "UPDATE 0"
            self._events[key]["lead_id"] = lead_id
            return "UPDATE 1"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        q = query.strip().upper()

        if q.startswith("INSERT INTO SCHEDULE_EVENTS"):
            # ingest_calendly_event: INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING
            (tenant_id, event_id, lead_id, visitor_id, email, name, starts_at, ends_at,
             timezone, status, calendar_ref, consent, source) = args
            existing_key = next(
                (
                    key for key, row in self._events.items()
                    if key[0] == tenant_id and row["calendar_ref"] == calendar_ref and row.get("source") == "calendly"
                ),
                None,
            )
            if existing_key is not None:
                row = self._events[existing_key]
                row["starts_at"] = starts_at
                row["ends_at"] = ends_at
                row["timezone"] = timezone
                row["email"] = email
                row["name"] = name
                row["status"] = "booked"
                if row.get("visitor_id") is None:
                    row["visitor_id"] = visitor_id
                return dict(row)
            new_row = {
                "tenant_id": tenant_id, "event_id": event_id, "lead_id": lead_id,
                "visitor_id": visitor_id, "email": email, "name": name,
                "starts_at": starts_at, "ends_at": ends_at, "timezone": timezone,
                "status": status, "calendar_ref": calendar_ref, "consent": consent,
                "created_at": _NOW, "source": source, "meet_url": None,
            }
            self._events[(tenant_id, event_id)] = new_row
            return dict(new_row)

        if "FROM AVAILABILITY" in q:
            tenant_id = args[0]
            return self._availability.get(tenant_id)

        if "FROM SCHEDULE_EVENTS" in q:
            if "VISITOR_ID = $2" in q:
                tenant_id, visitor_id, now = args
                rows = [row for row in self._events.values() if row["tenant_id"] == tenant_id and row["visitor_id"] == visitor_id and row["status"] == "booked" and row["starts_at"] > now]
                return min(rows, key=lambda row: row["starts_at"]) if rows else None
            tenant_id, event_id = args
            return self._events.get((tenant_id, event_id))

        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        q = query.strip().upper()

        if "FROM SCHEDULE_EVENTS" in q and "LEAD_ID = ANY" in q:
            tenant_id, lead_ids, visitor_ids = args[0], args[1], args[2]
            rows = [
                row for row in self._events.values()
                if row["tenant_id"] == tenant_id
                and (row["lead_id"] in lead_ids or row["visitor_id"] in visitor_ids)
            ]
            # args are [tenant_id, lead_ids, visitor_ids, (before,) limit] --
            # `before` is present only when the query text contains it.
            if "STARTS_AT < $" in q:
                before = args[3]
                rows = [r for r in rows if r["starts_at"] < before]
            rows.sort(key=lambda r: r["starts_at"], reverse=True)
            return rows

        if "FROM SCHEDULE_EVENTS" in q:
            tenant_id = args[0]
            rows = [
                row
                for row in self._events.values()
                if row["tenant_id"] == tenant_id and row["status"] == "booked"
            ]
            if len(args) >= 3:
                window_start, window_end = args[1], args[2]
                rows = [r for r in rows if window_start <= r["starts_at"] <= window_end]
            rows.sort(key=lambda r: r["starts_at"])
            return rows

        return []


@pytest.fixture
def stub_db() -> _StubDatabase:
    return _StubDatabase()


# ---------------------------------------------------------------------------
# upsert_availability / get_availability
# ---------------------------------------------------------------------------


async def test_upsert_and_get_availability_round_trip(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import Availability, get_availability, upsert_availability

        claims = _claims(tenant_id="tenant-abc")

        result = await upsert_availability(stub_db, claims, timezone="America/New_York", rules=_RULES)
        assert isinstance(result, Availability)
        assert result.timezone == "America/New_York"
        assert result.rules == _RULES

        fetched = await get_availability(stub_db, claims)
        assert isinstance(fetched, Availability)
        assert fetched.timezone == "America/New_York"
        assert fetched.rules == _RULES


async def test_upsert_availability_uses_positional_placeholders(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import upsert_availability

        claims = _claims(tenant_id="tenant-abc")
        await upsert_availability(stub_db, claims, timezone="UTC", rules=_RULES)

        query, args = stub_db.execute_calls[-1]
        assert "$1" in query
        assert "$2" in query
        assert "$3" in query
        assert ":" not in query
        assert args[0] == "tenant-abc"


async def test_get_availability_returns_none_when_unset(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_availability

        claims = _claims(tenant_id="tenant-abc")
        result = await get_availability(stub_db, claims)

        assert result is None


async def test_availability_cross_tenant_isolation(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_availability, upsert_availability

        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")

        await upsert_availability(stub_db, claims_a, timezone="UTC", rules=_RULES)
        result = await get_availability(stub_db, claims_b)

        assert result is None


async def test_upsert_availability_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import upsert_availability

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await upsert_availability(stub_db, global_claims, timezone="UTC", rules=_RULES)


async def test_get_availability_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_availability

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_availability(stub_db, global_claims)


# ---------------------------------------------------------------------------
# list_booked
# ---------------------------------------------------------------------------


async def test_list_booked_tenant_scoped_and_windowed(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, list_booked

        claims = _claims(tenant_id="tenant-abc")
        starts_at = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
        ends_at = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        await create_event(
            stub_db, claims,
            starts_at=starts_at, ends_at=ends_at, timezone="UTC",
            visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        booked = await list_booked(
            stub_db, claims,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 10, tzinfo=UTC),
        )

        assert booked == [(starts_at, ends_at)]


async def test_list_booked_excludes_outside_window(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, list_booked

        claims = _claims(tenant_id="tenant-abc")
        starts_at = datetime(2026, 6, 5, 14, 0, tzinfo=UTC)
        ends_at = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)
        await create_event(
            stub_db, claims,
            starts_at=starts_at, ends_at=ends_at, timezone="UTC",
            visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        booked = await list_booked(
            stub_db, claims,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 10, tzinfo=UTC),
        )

        assert booked == []


async def test_list_booked_cross_tenant_isolation(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, list_booked

        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")
        starts_at = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
        ends_at = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        await create_event(
            stub_db, claims_a,
            starts_at=starts_at, ends_at=ends_at, timezone="UTC",
            visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        booked = await list_booked(
            stub_db, claims_b,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 10, tzinfo=UTC),
        )

        assert booked == []


async def test_list_booked_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import list_booked

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await list_booked(
                stub_db, global_claims,
                window_start=datetime(2026, 1, 1, tzinfo=UTC),
                window_end=datetime(2026, 1, 10, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


async def test_create_event_inserts_tenant_scoped_row_with_uuid_event_id(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import ScheduleEvent, create_event

        claims = _claims(tenant_id="tenant-abc")
        starts_at = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
        ends_at = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        consent = {"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"}

        event = await create_event(
            stub_db, claims,
            starts_at=starts_at, ends_at=ends_at, timezone="UTC",
            visitor_id="visitor-1", lead_id="lead-1", consent=consent,
        )

        assert isinstance(event, ScheduleEvent)
        assert isinstance(event.event_id, str)
        assert len(event.event_id) == 32
        assert event.status == "booked"
        assert event.starts_at == starts_at
        assert event.ends_at == ends_at
        assert event.calendar_ref is None

        insert_query, insert_args = stub_db.execute_calls[-1]
        assert "insert into schedule_events" in insert_query.lower()
        assert "$1" in insert_query
        assert ":" not in insert_query
        assert insert_args[0] == "tenant-abc"


async def test_create_event_unique_violation_raises_slot_unavailable(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event

        claims = _claims(tenant_id="tenant-abc")
        starts_at = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
        ends_at = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        consent = {"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"}

        await create_event(
            stub_db, claims,
            starts_at=starts_at, ends_at=ends_at, timezone="UTC",
            visitor_id="visitor-1", lead_id=None, consent=consent,
        )

        with pytest.raises(ValidationError) as exc_info:
            await create_event(
                stub_db, claims,
                starts_at=starts_at, ends_at=ends_at, timezone="UTC",
                visitor_id="visitor-2", lead_id=None, consent=consent,
            )

        assert exc_info.value.code == "SLOT_UNAVAILABLE"


async def test_create_event_simulated_unique_violation_via_injection(stub_db: _StubDatabase) -> None:
    """A directly-injected asyncpg.UniqueViolationError is also caught -> SLOT_UNAVAILABLE."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event

        claims = _claims(tenant_id="tenant-abc")
        stub_db.raise_on_insert_event = asyncpg.UniqueViolationError("dup")

        with pytest.raises(ValidationError) as exc_info:
            await create_event(
                stub_db, claims,
                starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
                ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
                timezone="UTC", visitor_id="visitor-1", lead_id=None,
                consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
            )

        assert exc_info.value.code == "SLOT_UNAVAILABLE"


async def test_create_event_cross_tenant_no_conflict(stub_db: _StubDatabase) -> None:
    """The same starts_at can be booked independently by two different tenants."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event

        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")
        starts_at = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
        ends_at = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        consent = {"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"}

        event_a = await create_event(
            stub_db, claims_a, starts_at=starts_at, ends_at=ends_at, timezone="UTC",
            visitor_id="visitor-1", lead_id=None, consent=consent,
        )
        event_b = await create_event(
            stub_db, claims_b, starts_at=starts_at, ends_at=ends_at, timezone="UTC",
            visitor_id="visitor-2", lead_id=None, consent=consent,
        )

        assert event_a.event_id != event_b.event_id


async def test_create_event_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await create_event(
                stub_db, global_claims,
                starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
                ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
                timezone="UTC", visitor_id="visitor-1", lead_id=None,
                consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
            )


# ---------------------------------------------------------------------------
# get_event_contact (S9.2, Scope §6)
# ---------------------------------------------------------------------------


async def test_get_event_contact_returns_contact_fields(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import EventContact, create_event, get_event_contact

        claims = _claims(tenant_id="tenant-abc")
        event = await create_event(
            stub_db, claims,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="America/New_York", visitor_id="visitor-1", lead_id="lead-1",
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        contact = await get_event_contact(stub_db, claims, event.event_id)

        assert isinstance(contact, EventContact)
        assert contact.lead_id == "lead-1"
        assert contact.visitor_id == "visitor-1"
        assert contact.timezone == "America/New_York"
        assert contact.status == "booked"


async def test_get_event_contact_missing_event_returns_none(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_event_contact

        claims = _claims(tenant_id="tenant-abc")
        result = await get_event_contact(stub_db, claims, "event-does-not-exist")

        assert result is None


async def test_get_event_contact_cross_tenant_isolation(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, get_event_contact

        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")
        event = await create_event(
            stub_db, claims_a,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        result = await get_event_contact(stub_db, claims_b, event.event_id)

        assert result is None


async def test_get_event_contact_uses_positional_placeholders(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_event_contact

        claims = _claims(tenant_id="tenant-abc")
        await get_event_contact(stub_db, claims, "event-1")

        query, args = stub_db.fetchrow_calls[-1]
        assert "$1" in query
        assert "$2" in query
        assert ":" not in query
        assert args[0] == "tenant-abc"


async def test_get_event_contact_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_event_contact

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_event_contact(stub_db, global_claims, "event-1")


# ---------------------------------------------------------------------------
# get_upcoming_booking (SR-5, decision 7 — booking-awareness)
# ---------------------------------------------------------------------------


async def test_get_upcoming_booking_returns_soonest_future_booked_event(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import ScheduleEvent, create_event, get_upcoming_booking

        claims = _claims(tenant_id="tenant-abc")
        consent = {"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"}
        later = await create_event(
            stub_db, claims,
            starts_at=datetime(2099, 6, 1, 14, 0, tzinfo=UTC), ends_at=datetime(2099, 6, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None, consent=consent,
        )
        sooner = await create_event(
            stub_db, claims,
            starts_at=datetime(2099, 3, 1, 14, 0, tzinfo=UTC), ends_at=datetime(2099, 3, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None, consent=consent,
        )

        result = await get_upcoming_booking(stub_db, claims, "visitor-1")

        assert isinstance(result, ScheduleEvent)
        assert result.event_id == sooner.event_id
        assert result.event_id != later.event_id


async def test_get_upcoming_booking_ignores_past_events(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, get_upcoming_booking

        claims = _claims(tenant_id="tenant-abc")
        await create_event(
            stub_db, claims,
            starts_at=datetime(2020, 1, 1, 14, 0, tzinfo=UTC), ends_at=datetime(2020, 1, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        result = await get_upcoming_booking(stub_db, claims, "visitor-1")

        assert result is None


async def test_get_upcoming_booking_ignores_cancelled_events(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, get_upcoming_booking

        claims = _claims(tenant_id="tenant-abc")
        event = await create_event(
            stub_db, claims,
            starts_at=datetime(2099, 6, 1, 14, 0, tzinfo=UTC), ends_at=datetime(2099, 6, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )
        stub_db._events[("tenant-abc", event.event_id)]["status"] = "cancelled"

        result = await get_upcoming_booking(stub_db, claims, "visitor-1")

        assert result is None


async def test_get_upcoming_booking_returns_none_when_no_booking(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_upcoming_booking

        claims = _claims(tenant_id="tenant-abc")
        result = await get_upcoming_booking(stub_db, claims, "visitor-1")

        assert result is None


async def test_get_upcoming_booking_tenant_isolation(stub_db: _StubDatabase) -> None:
    """A booking under a different tenant (same visitor_id string) is never returned."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, get_upcoming_booking

        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")
        await create_event(
            stub_db, claims_a,
            starts_at=datetime(2099, 6, 1, 14, 0, tzinfo=UTC), ends_at=datetime(2099, 6, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        result = await get_upcoming_booking(stub_db, claims_b, "visitor-1")

        assert result is None


async def test_get_upcoming_booking_visitor_isolation(stub_db: _StubDatabase) -> None:
    """A booking under the same tenant but a different visitor_id is never returned."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, get_upcoming_booking

        claims = _claims(tenant_id="tenant-abc")
        await create_event(
            stub_db, claims,
            starts_at=datetime(2099, 6, 1, 14, 0, tzinfo=UTC), ends_at=datetime(2099, 6, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        result = await get_upcoming_booking(stub_db, claims, "visitor-2")

        assert result is None


async def test_get_upcoming_booking_uses_positional_placeholders_with_tenant_and_visitor(
    stub_db: _StubDatabase,
) -> None:
    """MANDATORY: the query must filter tenant_id AND visitor_id together, never visitor_id alone."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_upcoming_booking

        claims = _claims(tenant_id="tenant-abc")
        await get_upcoming_booking(stub_db, claims, "visitor-1")

        query, args = stub_db.fetchrow_calls[-1]
        assert "tenant_id = $1" in query.lower()
        assert "visitor_id = $2" in query.lower()
        assert "status = 'booked'" in query.lower()
        assert "starts_at > $3" in query.lower()
        assert ":" not in query
        assert args[0] == "tenant-abc"
        assert args[1] == "visitor-1"


async def test_get_upcoming_booking_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_upcoming_booking

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await get_upcoming_booking(stub_db, global_claims, "visitor-1")

        assert stub_db.fetchrow_calls == []


# ---------------------------------------------------------------------------
# SR-6: create_event source='native', ingest_calendly_event, cancel_calendly_event
# ---------------------------------------------------------------------------


async def test_create_event_sets_source_native(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event

        claims = _claims(tenant_id="tenant-abc")
        event = await create_event(
            stub_db, claims,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        assert event.source == "native"
        assert stub_db._events[("tenant-abc", event.event_id)]["source"] == "native"


async def test_ingest_calendly_event_sets_source_calendly(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import ingest_calendly_event

        event = await ingest_calendly_event(
            stub_db, "tenant-abc",
            calendly_uuid="uuid-1",
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", email="a@example.com", name="A", visitor_id="visitor-1",
        )

        assert event.source == "calendly"
        assert event.status == "booked"
        assert event.calendar_ref == "calendly:uuid-1"
        assert event.visitor_id == "visitor-1"
        assert event.lead_id is None  # Calendly bookings never create a CRM lead


async def test_ingest_calendly_event_reingest_same_uuid_updates_in_place(stub_db: _StubDatabase) -> None:
    """Idempotent re-delivery: same Calendly UUID -> exactly ONE row, updated in place."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import ingest_calendly_event

        first = await ingest_calendly_event(
            stub_db, "tenant-abc",
            calendly_uuid="uuid-1",
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", email="a@example.com", name="A", visitor_id="visitor-1",
        )
        second = await ingest_calendly_event(
            stub_db, "tenant-abc",
            calendly_uuid="uuid-1",
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", email="a@example.com", name="A", visitor_id=None,
        )

        assert first.event_id == second.event_id
        rows = [
            row for row in stub_db._events.values()
            if row["tenant_id"] == "tenant-abc" and row["calendar_ref"] == "calendly:uuid-1"
        ]
        assert len(rows) == 1
        # visitor_id preserved from the first ingest, never overwritten by a
        # later None (COALESCE semantics in the real SQL).
        assert second.visitor_id == "visitor-1"


async def test_ingest_calendly_event_no_correlation_visitor_id_null(stub_db: _StubDatabase) -> None:
    """Honest no-match (decision 5b): booking still ingested, visitor_id=NULL."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import ingest_calendly_event

        event = await ingest_calendly_event(
            stub_db, "tenant-abc",
            calendly_uuid="uuid-no-match",
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", email="nobody@example.com", name=None, visitor_id=None,
        )

        assert event.visitor_id is None
        assert event.status == "booked"  # never dropped


async def test_cancel_calendly_event_flips_status(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import cancel_calendly_event, ingest_calendly_event

        await ingest_calendly_event(
            stub_db, "tenant-abc",
            calendly_uuid="uuid-1",
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", email="a@example.com", name="A", visitor_id="visitor-1",
        )

        await cancel_calendly_event(stub_db, "tenant-abc", "uuid-1")

        row = next(iter(stub_db._events.values()))
        assert row["status"] == "cancelled"


async def test_cancel_calendly_event_unknown_uuid_is_noop_success(stub_db: _StubDatabase) -> None:
    """A cancel for an unknown UUID is a no-op success -- never an error, never a fabricated row."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import cancel_calendly_event

        await cancel_calendly_event(stub_db, "tenant-abc", "uuid-does-not-exist")

        assert stub_db._events == {}


async def test_get_upcoming_booking_returns_calendly_booking_with_visitor_id(
    stub_db: _StubDatabase,
) -> None:
    """A Calendly-sourced booking with a backfilled visitor_id is recognized
    by get_upcoming_booking for free -- the method itself is unchanged."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_upcoming_booking, ingest_calendly_event

        await ingest_calendly_event(
            stub_db, "tenant-abc",
            calendly_uuid="uuid-1",
            starts_at=datetime(2099, 6, 1, 14, 0, tzinfo=UTC),
            ends_at=datetime(2099, 6, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", email="a@example.com", name="A", visitor_id="visitor-1",
        )

        claims = _claims(tenant_id="tenant-abc")
        result = await get_upcoming_booking(stub_db, claims, "visitor-1")

        assert result is not None
        assert result.calendar_ref == "calendly:uuid-1"


async def test_get_upcoming_booking_calendly_row_visitor_id_null_not_returned(
    stub_db: _StubDatabase,
) -> None:
    """A Calendly row with visitor_id=NULL (no correlation match) is NOT
    returned by get_upcoming_booking for any visitor -- tenant+visitor
    isolation reasserted (MANDATORY)."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import get_upcoming_booking, ingest_calendly_event

        await ingest_calendly_event(
            stub_db, "tenant-abc",
            calendly_uuid="uuid-1",
            starts_at=datetime(2099, 6, 1, 14, 0, tzinfo=UTC),
            ends_at=datetime(2099, 6, 1, 14, 30, tzinfo=UTC),
            timezone="UTC", email="a@example.com", name="A", visitor_id=None,
        )

        claims = _claims(tenant_id="tenant-abc")
        result = await get_upcoming_booking(stub_db, claims, "visitor-1")

        assert result is None


# ---------------------------------------------------------------------------
# set_event_lead_id (SR-9.1)
# ---------------------------------------------------------------------------


async def test_set_event_lead_id_updates_the_event(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, set_event_lead_id

        claims = _claims(tenant_id="tenant-abc")
        event = await create_event(
            stub_db, claims,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        await set_event_lead_id(stub_db, claims, event.event_id, "lead-abc")

        stored = stub_db._events[(claims.tenant_id, event.event_id)]
        assert stored["lead_id"] == "lead-abc"


async def test_set_event_lead_id_uses_tenant_scoped_positional_sql(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, set_event_lead_id

        claims = _claims(tenant_id="tenant-abc")
        event = await create_event(
            stub_db, claims,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        await set_event_lead_id(stub_db, claims, event.event_id, "lead-abc")

        query, args = stub_db.execute_calls[-1]
        assert "update schedule_events" in query.lower()
        assert "set lead_id" in query.lower()
        assert "where" in query.lower()
        assert "tenant_id" in query.lower()
        assert "event_id" in query.lower()
        assert ":" not in query
        assert "lead-abc" in args
        assert claims.tenant_id in args
        assert event.event_id in args


async def test_set_event_lead_id_cross_tenant_no_op(stub_db: _StubDatabase) -> None:
    """A tenant-B caller cannot link a lead onto tenant-A's event."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, set_event_lead_id

        claims_a = _claims(tenant_id="tenant-a")
        claims_b = _claims(tenant_id="tenant-b")
        event = await create_event(
            stub_db, claims_a,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id="visitor-1", lead_id=None,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        await set_event_lead_id(stub_db, claims_b, event.event_id, "lead-xyz")

        stored = stub_db._events[(claims_a.tenant_id, event.event_id)]
        assert stored["lead_id"] is None


async def test_set_event_lead_id_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import set_event_lead_id

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await set_event_lead_id(stub_db, global_claims, "some-event-id", "lead-1")


# ---------------------------------------------------------------------------
# list_events_for_timeline (SR-9.3 J8/D5)
# ---------------------------------------------------------------------------


async def test_list_events_for_timeline_reachable_by_lead_id(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, list_events_for_timeline

        claims = _claims(role=Role.CLIENT_ADMIN)
        event = await create_event(
            stub_db, claims,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id=None, lead_id="lead-1",
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        results = await list_events_for_timeline(
            stub_db, claims, lead_ids=["lead-1"], visitor_ids=[],
        )
        assert [r.event_id for r in results] == [event.event_id]


async def test_list_events_for_timeline_calendly_reachable_only_by_visitor_id(
    stub_db: _StubDatabase,
) -> None:
    """A Calendly-sourced event never has a lead_id (J8) -- it must still
    appear via the visitor_id OR path."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import ingest_calendly_event, list_events_for_timeline

        claims = _claims(role=Role.CLIENT_ADMIN)
        event = await ingest_calendly_event(
            stub_db, claims.tenant_id,
            calendly_uuid="cal-uuid-1",
            starts_at=datetime(2026, 1, 6, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 6, 14, 30, tzinfo=UTC),
            timezone="UTC", email="visitor@example.com", name=None,
            visitor_id="visitor-calendly-1",
        )
        assert event.lead_id is None

        # No lead_id match -> still found via visitor_id.
        results = await list_events_for_timeline(
            stub_db, claims, lead_ids=["some-other-lead"], visitor_ids=["visitor-calendly-1"],
        )
        assert [r.event_id for r in results] == [event.event_id]

        # Nothing shared -> not found.
        no_match = await list_events_for_timeline(
            stub_db, claims, lead_ids=["some-other-lead"], visitor_ids=["some-other-visitor"],
        )
        assert no_match == []


async def test_list_events_for_timeline_tenant_isolation_same_lead_id(
    stub_db: _StubDatabase,
) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import create_event, list_events_for_timeline

        claims_a = _claims(tenant_id="tenant-a", role=Role.CLIENT_ADMIN)
        claims_b = _claims(tenant_id="tenant-b", role=Role.CLIENT_ADMIN)
        shared_lead_id = "lead-shared"

        event_a = await create_event(
            stub_db, claims_a,
            starts_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            timezone="UTC", visitor_id=None, lead_id=shared_lead_id,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )
        event_b = await create_event(
            stub_db, claims_b,
            starts_at=datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 5, 15, 30, tzinfo=UTC),
            timezone="UTC", visitor_id=None, lead_id=shared_lead_id,
            consent={"granted": True, "purpose": "booking", "text": "OK", "captured_at": "x"},
        )

        results_a = await list_events_for_timeline(
            stub_db, claims_a, lead_ids=[shared_lead_id], visitor_ids=[],
        )
        results_b = await list_events_for_timeline(
            stub_db, claims_b, lead_ids=[shared_lead_id], visitor_ids=[],
        )
        assert [r.event_id for r in results_a] == [event_a.event_id]
        assert [r.event_id for r in results_b] == [event_b.event_id]


async def test_list_events_for_timeline_rejects_global_caller(stub_db: _StubDatabase) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.scheduling.repository import list_events_for_timeline

        global_claims = AuthClaims(subject="admin-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

        with pytest.raises(ValidationError):
            await list_events_for_timeline(stub_db, global_claims, lead_ids=[], visitor_ids=[])
