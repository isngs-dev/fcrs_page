"""Unit tests for GET /public/schedule/slots + POST /public/schedule/book.

Covers:
- GET /slots returns open slots for the tenant (visitor session).
- No availability configured -> [] (200, not an error).
- POST /book valid + consent -> 201 event status:"booked".
- Consent false/omitted -> 422 CONSENT_REQUIRED, nothing stored.
- Booking a non-open time -> 422 SLOT_UNAVAILABLE.
- Double-book (second book of the same start) -> 422 SLOT_UNAVAILABLE.
- tenant_id/visitor_id come from the session, never the body.
- No bearer -> 401.
- Tenant isolation: tenant A's slots/events never reflect tenant B.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"
_OTHER_TENANT_ID = "tenant-xyz-999"


def _next_monday_iso() -> str:
    """A Monday at least a week in the future, so slots are never in the past."""
    today = datetime.now(UTC).date()
    days_ahead = (7 - today.weekday()) % 7 or 7  # next Monday, at least 1 day out
    days_ahead += 7  # push another week out for safety margin
    monday = today + timedelta(days=days_ahead)
    return monday.isoformat()


_MONDAY = _next_monday_iso()

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": _TEST_JWT_SECRET,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}

@pytest.fixture(autouse=True)
def _mock_lead_qualification_enqueue() -> Any:
    """Prevent every booking test from making a real (slow, ultimately
    failing against the fake ``stub-host`` broker) Celery dispatch for the
    new ``classify_lead_email(...)`` enqueue in the booking-lead autolink's
    genuinely-new-lead branch (``api.scheduling.routes``). Before this
    feature, that route had no Celery-task side effect at all, so no test
    here needed to mock one -- autouse keeps every existing test function
    unchanged rather than touching each of the dozens of tests below that
    incidentally create a fresh lead while booking.
    """
    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False), patch(
        "api.scheduling.routes.classify_lead_email"
    ):
        yield


_RULES = {
    "slot_minutes": 30,
    "buffer_minutes": 0,
    "weekly_hours": {
        "mon": [["09:00", "17:00"]], "tue": [["09:00", "17:00"]],
        "wed": [["09:00", "17:00"]], "thu": [["09:00", "17:00"]],
        "fri": [["09:00", "17:00"]], "sat": [], "sun": [],
    },
}


class _StubDatabase:
    """In-memory stub database backing the scheduling routes for these tests."""

    def __init__(self) -> None:
        self._availability: dict[str, dict[str, Any]] = {}
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self._calendar_configs: dict[str, dict[str, Any]] = {}
        self._reminder_jobs: dict[str, dict[str, Any]] = {}
        self._handoff_intents: list[dict[str, Any]] = []
        # leads/lead_activities (SR-9.1 booking-lead autolink)
        self._leads: dict[tuple[str, str], dict[str, Any]] = {}
        self._lead_activities: dict[tuple[str, str], dict[str, Any]] = {}
        # When set, create_lead/set_event_lead_id raise this (SR-9.1 C1 test).
        self.raise_on_lead_write: Exception | None = None

    def seed_availability(self, *, tenant_id: str, timezone: str = "UTC", rules: dict[str, Any] = _RULES) -> None:
        self._availability[tenant_id] = {
            "tenant_id": tenant_id,
            "timezone": timezone,
            "rules": rules,
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

    def seed_calendly(
        self, *, tenant_id: str, scheduling_url: str = "https://calendly.com/acme/intro",
        enabled: bool = True,
    ) -> None:
        self._calendar_configs[tenant_id] = {
            "provider": "calendly", "calendar_id": None,
            "credentials_ciphertext": None, "busy": [],
            "enabled": enabled, "scheduling_url": scheduling_url,
        }

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()

        if q.startswith("INSERT INTO LEADS"):
            if self.raise_on_lead_write is not None:
                exc = self.raise_on_lead_write
                self.raise_on_lead_write = None
                raise exc
            (tenant_id, lead_id, visitor_id, name, email, phone, status, stage,
             qualification_score, consent, assigned_agent_id, source) = args
            self._leads[(tenant_id, lead_id)] = {
                "tenant_id": tenant_id, "lead_id": lead_id, "visitor_id": visitor_id,
                "name": name, "email": email, "phone": phone, "status": status,
                "stage": stage, "qualification_score": qualification_score,
                "consent": consent, "assigned_agent_id": assigned_agent_id,
                "source": source, "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO LEAD_ACTIVITIES"):
            tenant_id, activity_id, lead_id, activity_type, payload, actor = args
            self._lead_activities[(tenant_id, activity_id)] = {
                "tenant_id": tenant_id, "activity_id": activity_id, "lead_id": lead_id,
                "type": activity_type, "payload": payload, "actor": actor,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            return "INSERT 0 1"

        if q.startswith("UPDATE SCHEDULE_EVENTS SET LEAD_ID"):
            if self.raise_on_lead_write is not None:
                exc = self.raise_on_lead_write
                self.raise_on_lead_write = None
                raise exc
            lead_id, tenant_id, event_id = args
            key = (tenant_id, event_id)
            if key in self._events:
                self._events[key]["lead_id"] = lead_id
            return "UPDATE 1"

        if q.startswith("INSERT INTO AVAILABILITY"):
            tenant_id, timezone, rules = args
            self._availability[tenant_id] = {
                "tenant_id": tenant_id, "timezone": timezone, "rules": rules,
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO SCHEDULE_EVENTS"):
            (tenant_id, event_id, lead_id, visitor_id, email, name, starts_at, ends_at,
             timezone, status, calendar_ref, consent, source) = args
            for (t_id, _e_id), existing in self._events.items():
                if t_id == tenant_id and existing["starts_at"] == starts_at and existing["status"] == "booked":
                    raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
            self._events[(tenant_id, event_id)] = {
                "tenant_id": tenant_id, "event_id": event_id, "lead_id": lead_id,
                "visitor_id": visitor_id, "email": email, "name": name, "starts_at": starts_at, "ends_at": ends_at,
                "timezone": timezone, "status": status, "calendar_ref": calendar_ref,
                "consent": consent, "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "source": source,
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO TENANT_CALENDAR_CONFIGS"):
            (tenant_id, provider, calendar_id, credentials_ciphertext, busy, enabled,
             scheduling_url) = args
            self._calendar_configs[tenant_id] = {
                "provider": provider, "calendar_id": calendar_id,
                "credentials_ciphertext": credentials_ciphertext, "busy": busy,
                "enabled": enabled, "scheduling_url": scheduling_url,
            }
            return "INSERT 0 1"

        if q.startswith("INSERT INTO CALENDLY_HANDOFF_INTENTS"):
            tenant_id, visitor_id, email, ttl_seconds = args
            self._handoff_intents.append(
                {"tenant_id": tenant_id, "visitor_id": visitor_id, "email": email}
            )
            return "INSERT 0 1"

        if q.startswith("UPDATE SCHEDULE_EVENTS"):
            tenant_id, event_id, calendar_ref, meet_url = args
            key = (tenant_id, event_id)
            if key in self._events:
                self._events[key]["calendar_ref"] = calendar_ref
                self._events[key]["meet_url"] = meet_url
            return "UPDATE 1"

        if q.startswith("DELETE FROM SCHEDULE_EVENTS"):
            tenant_id, event_id = args
            self._events.pop((tenant_id, event_id), None)
            # Simulate the real FK ON DELETE CASCADE (migration 0020): deleting
            # an event removes its reminder_jobs rows too.
            for job_id in [
                jid for jid, job in self._reminder_jobs.items()
                if job["tenant_id"] == tenant_id and job["event_id"] == event_id
            ]:
                del self._reminder_jobs[job_id]
            return "DELETE 1"

        if q.startswith("INSERT INTO REMINDER_JOBS"):
            job_id, tenant_id, event_id, offset, run_at, status = args
            key = (tenant_id, event_id, offset)
            existing = next(
                (j for j in self._reminder_jobs.values()
                 if (j["tenant_id"], j["event_id"], j["offset"]) == key),
                None,
            )
            if existing is not None:
                return "INSERT 0 0"
            self._reminder_jobs[job_id] = {
                "job_id": job_id, "tenant_id": tenant_id, "event_id": event_id,
                "offset": offset, "run_at": run_at, "status": status, "attempts": 0,
                "last_error": None, "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            return "INSERT 0 1"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM LEADS" in q and "VISITOR_ID = $2" in q:
            tenant_id, visitor_id = args
            matches = [
                row for row in self._leads.values()
                if row["tenant_id"] == tenant_id and row["visitor_id"] == visitor_id
            ]
            if not matches:
                return None
            matches.sort(key=lambda r: r["created_at"], reverse=True)
            return matches[0]
        if "COUNT(*)" in q and "FROM LEADS" in q:
            tenant_id = args[0]
            total = sum(1 for row in self._leads.values() if row["tenant_id"] == tenant_id)
            return {"count": total}
        if "FROM AVAILABILITY" in q:
            tenant_id = args[0]
            return self._availability.get(tenant_id)
        if "FROM TENANT_CALENDAR_CONFIGS" in q:
            tenant_id = args[0]
            return self._calendar_configs.get(tenant_id)
        if "FROM SCHEDULE_EVENTS" in q and "VISITOR_ID = $2" in q:
            tenant_id, visitor_id, now = args
            rows = [row for row in self._events.values() if row["tenant_id"] == tenant_id and row["visitor_id"] == visitor_id and row["status"] == "booked" and row["starts_at"] > now]
            return min(rows, key=lambda row: row["starts_at"]) if rows else None
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM LEADS" in q:
            tenant_id = args[0]
            rows = [row for row in self._leads.values() if row["tenant_id"] == tenant_id]
            rows.sort(key=lambda r: (r["created_at"], r["lead_id"]), reverse=True)
            return rows
        if "FROM SCHEDULE_EVENTS" in q:
            tenant_id = args[0]
            rows = [
                row for row in self._events.values()
                if row["tenant_id"] == tenant_id and row["status"] == "booked"
            ]
            if len(args) >= 3:
                window_start, window_end = args[1], args[2]
                rows = [r for r in rows if window_start <= r["starts_at"] <= window_end]
            rows.sort(key=lambda r: r["starts_at"])
            return rows
        if "FROM REMINDER_JOBS" in q:
            tenant_id, event_id = args
            rows = [
                j for j in self._reminder_jobs.values()
                if j["tenant_id"] == tenant_id and j["event_id"] == event_id
            ]
            rows.sort(key=lambda j: j["run_at"])
            return rows
        return []

    async def close(self) -> None:
        pass


class _StubRedis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        pass

    async def getdel(self, key: str) -> str | None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app(db: _StubDatabase) -> Any:
    _reset_settings()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    return app


def _visitor_token(tenant_id: str = _TENANT_ID, visitor_id: str = "visitor-123") -> str:
    claims = AuthClaims(subject=visitor_id, role=Role.VISITOR, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


def _admin_token(tenant_id: str = _TENANT_ID) -> str:
    claims = AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


async def _configure_calendar(client: AsyncClient, token: str, **overrides: Any) -> Any:
    body: dict[str, Any] = {
        "provider": "stub",
        "calendar_id": "dev",
        "credentials": "stub-token-value",
        "enabled": True,
        "busy": [],
    }
    body.update(overrides)
    response = await client.put(
        "/admin/schedule/calendar", json=body, cookies={"access_token": token}
    )
    assert response.status_code == 200
    return response


# ---------------------------------------------------------------------------
# GET /public/schedule/slots
# ---------------------------------------------------------------------------


async def test_get_slots_returns_open_slots_for_tenant() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.get(
            f"/public/schedule/slots?date_from={_MONDAY}&date_to={_MONDAY}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 16
    assert data[0]["starts_at"].startswith(f"{_MONDAY}T09:00:00")


async def test_get_slots_no_availability_returns_empty_list() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.get(
            "/public/schedule/slots",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == []


async def test_get_slots_no_bearer_returns_401() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/public/schedule/slots")

    assert response.status_code == 401


async def test_get_slots_tenant_isolation() -> None:
    """Tenant A's slots do not reflect tenant B's availability/bookings."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    # tenant B has no availability configured
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token_b = _visitor_token(tenant_id=_OTHER_TENANT_ID)
        response = await client.get(
            f"/public/schedule/slots?date_from={_MONDAY}&date_to={_MONDAY}",
            headers={"Authorization": f"Bearer {token_b}"},
        )

    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# POST /public/schedule/book
# ---------------------------------------------------------------------------


def _book_body(starts_at: str = f"{_MONDAY}T09:00:00+00:00") -> dict[str, Any]:
    return {
        "starts_at": starts_at,
        "timezone": "UTC",
        "consent": {"granted": True, "purpose": "booking", "text": "I agree."},
    }


async def test_post_book_valid_consent_returns_201_booked() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book",
            json=_book_body(),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "booked"
    assert "event_id" in data
    assert "tenant_id" not in data
    assert "visitor_id" not in data


async def test_post_book_consent_false_returns_422_and_nothing_stored() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    body = _book_body()
    body["consent"] = {"granted": False, "purpose": "booking", "text": "no"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "CONSENT_REQUIRED"
    assert db._events == {}


async def test_post_book_consent_omitted_returns_422() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    body = {"starts_at": f"{_MONDAY}T09:00:00+00:00", "timezone": "UTC"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "CONSENT_REQUIRED"
    assert db._events == {}


async def test_post_book_non_open_time_returns_slot_unavailable() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    # 09:15 is not a slot boundary for a 30-minute grid starting at 09:00.
    body = _book_body(starts_at=f"{_MONDAY}T09:15:00+00:00")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "SLOT_UNAVAILABLE"


async def test_post_book_no_availability_returns_slot_unavailable() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "SLOT_UNAVAILABLE"


async def test_post_book_double_book_returns_slot_unavailable() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        first = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )
        second = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert first.status_code == 201
    assert second.status_code == 422
    assert second.json()["error_code"] == "SLOT_UNAVAILABLE"


async def test_post_book_uses_claims_visitor_and_tenant_not_body() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    body = _book_body()
    body["tenant_id"] = "tenant-fake"
    body["visitor_id"] = "visitor-fake"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(tenant_id=_TENANT_ID, visitor_id="visitor-real")
        response = await client.post(
            "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    stored = next(iter(db._events.values()))
    assert stored["tenant_id"] == _TENANT_ID
    assert stored["visitor_id"] == "visitor-real"


async def test_post_book_no_bearer_returns_401() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/public/schedule/book", json=_book_body())

    assert response.status_code == 401


async def test_post_book_cross_tenant_slot_independent() -> None:
    """Tenant A booking a start does not block tenant B from booking the same start."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    db.seed_availability(tenant_id=_OTHER_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token_a = _visitor_token(tenant_id=_TENANT_ID)
        token_b = _visitor_token(tenant_id=_OTHER_TENANT_ID)
        first = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token_a}"}
        )
        second = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token_b}"}
        )

    assert first.status_code == 201
    assert second.status_code == 201


# ---------------------------------------------------------------------------
# Calendar sync (S8.2)
# ---------------------------------------------------------------------------


async def test_get_slots_excludes_calendar_busy_interval() -> None:
    """A StubCalendarProvider busy interval is subtracted like a booked event."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = _admin_token()
        await _configure_calendar(
            client, admin_token,
            busy=[{"start": f"{_MONDAY}T09:00:00Z", "end": f"{_MONDAY}T09:30:00Z"}],
        )

        visitor_token = _visitor_token()
        response = await client.get(
            f"/public/schedule/slots?date_from={_MONDAY}&date_to={_MONDAY}",
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert not any(slot["starts_at"].startswith(f"{_MONDAY}T09:00:00") for slot in data)
    assert len(data) == 15  # 16 native slots minus the one excluded by free-busy


async def test_get_slots_freebusy_error_degrades_to_native_200() -> None:
    """An unusable calendar config (unknown provider) degrades to native slots, not a 500."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = _admin_token()
        await _configure_calendar(client, admin_token, provider="not-a-real-provider")

        visitor_token = _visitor_token()
        response = await client.get(
            f"/public/schedule/slots?date_from={_MONDAY}&date_to={_MONDAY}",
            headers={"Authorization": f"Bearer {visitor_token}"},
        )

    assert response.status_code == 200
    assert len(response.json()) == 16  # native slots, unaffected by the broken calendar config


async def test_get_slots_no_calendar_configured_is_native_s81_behavior() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.get(
            f"/public/schedule/slots?date_from={_MONDAY}&date_to={_MONDAY}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert len(response.json()) == 16


async def test_post_book_with_calendar_creates_and_persists_calendar_ref() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = _admin_token()
        await _configure_calendar(client, admin_token)

        visitor_token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {visitor_token}"}
        )

    assert response.status_code == 201
    stored = next(iter(db._events.values()))
    assert stored["status"] == "booked"
    event_id = response.json()["event_id"]
    assert stored["calendar_ref"] == f"stub:stub-{event_id}"
    # StubCalendarProvider (SR-22) returns a deterministic fake Meet URL --
    # proves it's actually persisted onto the booking, not just returned and
    # dropped.
    assert stored["meet_url"] == f"https://meet.google.com/stub-{event_id}"


async def test_post_book_calendar_sync_failure_compensates_no_orphan() -> None:
    """A calendar create_event failure deletes the row and raises CALENDAR_SYNC_FAILED."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = _admin_token()
        # An unknown provider makes calendar_provider_for raise inside the
        # booking route's calendar-sync try block (S8.2 decision 4).
        await _configure_calendar(client, admin_token, provider="not-a-real-provider")

        visitor_token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {visitor_token}"}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "CALENDAR_SYNC_FAILED"
    assert db._events == {}  # no orphan row


async def test_post_book_no_calendar_configured_native_path_calendar_ref_none() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    stored = next(iter(db._events.values()))
    assert stored["calendar_ref"] is None


async def test_post_book_calendar_disabled_skips_sync_calendar_ref_none() -> None:
    """A configured-but-disabled calendar is not synced (calendar_ref stays null)."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = _admin_token()
        await _configure_calendar(client, admin_token, enabled=False)

        visitor_token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {visitor_token}"}
        )

    assert response.status_code == 201
    stored = next(iter(db._events.values()))
    assert stored["calendar_ref"] is None


async def test_calendar_config_tenant_isolation_freebusy() -> None:
    """Tenant A's calendar busy interval never affects tenant B's slots."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    db.seed_availability(tenant_id=_OTHER_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token_a = _admin_token(tenant_id=_TENANT_ID)
        await _configure_calendar(
            client, admin_token_a,
            busy=[{"start": f"{_MONDAY}T09:00:00Z", "end": f"{_MONDAY}T09:30:00Z"}],
        )

        visitor_token_b = _visitor_token(tenant_id=_OTHER_TENANT_ID)
        response = await client.get(
            f"/public/schedule/slots?date_from={_MONDAY}&date_to={_MONDAY}",
            headers={"Authorization": f"Bearer {visitor_token_b}"},
        )

    assert response.status_code == 200
    assert len(response.json()) == 16  # tenant B has no calendar configured -- unaffected


async def test_calendar_config_tenant_isolation_booking() -> None:
    """Tenant A's calendar config never syncs tenant B's booking."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    db.seed_availability(tenant_id=_OTHER_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token_a = _admin_token(tenant_id=_TENANT_ID)
        await _configure_calendar(client, admin_token_a)

        visitor_token_b = _visitor_token(tenant_id=_OTHER_TENANT_ID)
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {visitor_token_b}"}
        )

    assert response.status_code == 201
    stored = next(iter(db._events.values()))
    assert stored["calendar_ref"] is None  # tenant B has no calendar -- native booking


# ---------------------------------------------------------------------------
# Reminder jobs (S8.3)
# ---------------------------------------------------------------------------


async def test_post_book_creates_three_reminder_jobs() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    event_id = response.json()["event_id"]
    reminders = [j for j in db._reminder_jobs.values() if j["event_id"] == event_id]
    assert len(reminders) == 3
    assert {j["offset"] for j in reminders} == {"3d", "24h", "1h"}
    assert all(j["tenant_id"] == _TENANT_ID for j in reminders)


async def test_post_book_reminder_creation_happens_before_calendar_sync() -> None:
    """create_reminder_jobs is called with the event's event_id + starts_at,
    before the S8.2 calendar sync step (spy on call order)."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    call_order: list[str] = []

    async def _spy_create_reminder_jobs(db_: Any, claims_: Any, **kwargs: Any) -> list[Any]:
        call_order.append("create_reminder_jobs")
        assert "event_id" in kwargs
        assert "starts_at" in kwargs
        from api.scheduling.reminder_repository import create_reminder_jobs as _real

        return await _real(db_, claims_, **kwargs)

    async def _spy_calendar_provider_for_async(*args: Any, **kwargs: Any) -> Any:
        call_order.append("calendar_provider_for_async")
        from api.scheduling.calendar import calendar_provider_for_async as _real

        return await _real(*args, **kwargs)

    with (
        patch("api.scheduling.routes.create_reminder_jobs", side_effect=_spy_create_reminder_jobs),
        patch(
            "api.scheduling.routes.calendar_provider_for_async",
            side_effect=_spy_calendar_provider_for_async,
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            admin_token = _admin_token()
            await _configure_calendar(client, admin_token)

            visitor_token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(),
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 201
    assert call_order == ["create_reminder_jobs", "calendar_provider_for_async"]


async def test_post_book_calendar_sync_failure_cascades_reminder_rows() -> None:
    """A CALENDAR_SYNC_FAILED compensation (delete_event) leaves no reminder rows."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = _admin_token()
        await _configure_calendar(client, admin_token, provider="not-a-real-provider")

        visitor_token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {visitor_token}"}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "CALENDAR_SYNC_FAILED"
    assert db._events == {}
    assert db._reminder_jobs == {}  # cascaded away with the compensated event


async def test_post_book_no_calendar_configured_still_creates_three_reminder_rows() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    event_id = response.json()["event_id"]
    reminders = [j for j in db._reminder_jobs.values() if j["event_id"] == event_id]
    assert len(reminders) == 3


# ---------------------------------------------------------------------------
# Booking confirmation enqueue (S9.2, Scope §8)
# ---------------------------------------------------------------------------


async def test_post_book_resolvable_recipient_enqueues_confirmation_and_delays_once() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    with (
        patch(
            "api.scheduling.routes.resolve_event_recipient",
            new=AsyncMock(return_value="lead@example.com"),
        ) as mock_resolve,
        patch(
            "api.scheduling.routes.enqueue_notification",
            new=AsyncMock(return_value="job-confirm-1"),
        ) as mock_enqueue,
        patch("api.scheduling.routes.send_notification") as mock_task,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    event_id = response.json()["event_id"]
    mock_resolve.assert_awaited_once()
    mock_enqueue.assert_awaited_once()
    _, kwargs = mock_enqueue.call_args
    assert kwargs["dedupe_key"] == f"booking_confirm:{event_id}"
    assert kwargs["channel"] == "email"
    mock_task.delay.assert_called_once()
    _, delay_kwargs = mock_task.delay.call_args
    assert delay_kwargs["job_id"] == "job-confirm-1"


async def test_post_book_calendly_link_configured_included_in_confirmation_body() -> None:
    """A Calendly row with `enabled=False` (native flow stays primary, no
    calendar-sync attempt) still surfaces its scheduling_url in the
    confirmation email as the reschedule link."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    db.seed_calendly(
        tenant_id=_TENANT_ID, scheduling_url="https://calendly.com/acme/intro", enabled=False,
    )
    app = _build_app(db)

    with (
        patch(
            "api.scheduling.routes.resolve_event_recipient",
            new=AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.scheduling.routes.enqueue_notification",
            new=AsyncMock(return_value="job-confirm-link"),
        ) as mock_enqueue,
        patch("api.scheduling.routes.send_notification"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    mock_enqueue.assert_awaited_once()
    _, kwargs = mock_enqueue.call_args
    assert "https://calendly.com/acme/intro" in kwargs["body"]


async def test_post_book_no_calendly_configured_confirmation_body_has_no_link() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    with (
        patch(
            "api.scheduling.routes.resolve_event_recipient",
            new=AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.scheduling.routes.enqueue_notification",
            new=AsyncMock(return_value="job-confirm-nolink"),
        ) as mock_enqueue,
        patch("api.scheduling.routes.send_notification"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    mock_enqueue.assert_awaited_once()
    _, kwargs = mock_enqueue.call_args
    assert "calendly.com" not in kwargs["body"]
    assert "contact us" in kwargs["body"]


async def test_post_book_confirmation_enqueue_carries_autolinked_lead_id() -> None:
    """SR-9.3 D4/Scope §3: the successful autolink's lead_id (already in
    scope from SR-9.1's create-or-link block) is passed onto the booking
    confirmation's enqueue_notification call -- zero extra queries needed."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    with (
        patch(
            "api.scheduling.routes.resolve_event_recipient",
            new=AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.scheduling.routes.enqueue_notification",
            new=AsyncMock(return_value="job-confirm-2"),
        ) as mock_enqueue,
        patch("api.scheduling.routes.send_notification"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    event_id = response.json()["event_id"]
    stored_event = next(v for v in db._events.values() if v["event_id"] == event_id)

    mock_enqueue.assert_awaited_once()
    _, kwargs = mock_enqueue.call_args
    assert kwargs["lead_id"] == stored_event["lead_id"]
    assert kwargs["lead_id"] is not None


async def test_post_book_degraded_autolink_still_enqueues_confirmation_with_null_lead_id() -> None:
    """SR-9.3 D4/Scope §3: a degraded autolink (create_lead/set_event_lead_id
    raises) passes lead_id=None to enqueue_notification rather than raising
    -- the confirmation notification is never lost because the link failed."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    db.raise_on_lead_write = RuntimeError("lead write unavailable")
    app = _build_app(db)

    with (
        patch(
            "api.scheduling.routes.resolve_event_recipient",
            new=AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.scheduling.routes.enqueue_notification",
            new=AsyncMock(return_value="job-confirm-3"),
        ) as mock_enqueue,
        patch("api.scheduling.routes.send_notification"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    mock_enqueue.assert_awaited_once()
    _, kwargs = mock_enqueue.call_args
    assert kwargs["lead_id"] is None


async def test_post_book_no_recipient_skips_enqueue_still_201() -> None:
    """No resolvable recipient (default stub DB) -> no enqueue, booking still 201."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    with patch("api.scheduling.routes.enqueue_notification") as mock_enqueue:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    mock_enqueue.assert_not_called()


async def test_post_book_enqueue_raises_degrades_still_201() -> None:
    """An enqueue that raises is best-effort -- booking still 201 (never 500)."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    with (
        patch(
            "api.scheduling.routes.resolve_event_recipient",
            new=AsyncMock(return_value="lead@example.com"),
        ),
        patch(
            "api.scheduling.routes.enqueue_notification",
            new=AsyncMock(side_effect=RuntimeError("db unavailable")),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201


async def test_post_book_calendar_sync_failure_enqueues_no_confirmation() -> None:
    """A CALENDAR_SYNC_FAILED compensation path enqueues NO confirmation."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    with patch("api.scheduling.routes.enqueue_notification") as mock_enqueue:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            admin_token = _admin_token()
            await _configure_calendar(client, admin_token, provider="not-a-real-provider")

            visitor_token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(),
                headers={"Authorization": f"Bearer {visitor_token}"},
            )

    assert response.status_code == 422
    assert response.json()["error_code"] == "CALENDAR_SYNC_FAILED"
    mock_enqueue.assert_not_called()


async def test_post_book_repeat_dedupe_key_does_not_double_delay() -> None:
    """Idempotency (MANDATORY): a repeat with the same dedupe target
    (enqueue_notification -> None) results in NO second .delay()."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    with (
        patch(
            "api.scheduling.routes.resolve_event_recipient",
            new=AsyncMock(return_value="lead@example.com"),
        ),
        patch("api.scheduling.routes.enqueue_notification", new=AsyncMock(return_value=None)),
        patch("api.scheduling.routes.send_notification") as mock_task,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token()
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    mock_task.delay.assert_not_called()


# ---------------------------------------------------------------------------
# SR-5: invite contact + booking awareness
# ---------------------------------------------------------------------------


async def test_post_book_stores_invite_email_and_name() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    body = _book_body()
    body.update({"email": "invite@example.com", "name": "Visitor Name"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {_visitor_token()}"}
        )

    assert response.status_code == 201
    stored = next(iter(db._events.values()))
    assert stored["email"] == "invite@example.com"
    assert stored["name"] == "Visitor Name"
    assert "email" not in response.json()


async def test_availability_summary_is_tenant_and_visitor_scoped() -> None:
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    db.seed_availability(tenant_id=_OTHER_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/public/schedule/book", json=_book_body(),
            headers={"Authorization": f"Bearer {_visitor_token(visitor_id='visitor-a')}"},
        )
        assert first.status_code == 201
        own = await client.get(
            "/public/schedule/availability-summary",
            headers={"Authorization": f"Bearer {_visitor_token(visitor_id='visitor-a')}"},
        )
        other_visitor = await client.get(
            "/public/schedule/availability-summary",
            headers={"Authorization": f"Bearer {_visitor_token(visitor_id='visitor-b')}"},
        )
        other_tenant = await client.get(
            "/public/schedule/availability-summary",
            headers={"Authorization": f"Bearer {_visitor_token(tenant_id=_OTHER_TENANT_ID, visitor_id='visitor-a')}"},
        )

    assert own.status_code == 200
    assert own.json()["action"] == "schedule_cta"
    assert own.json()["existing_booking"] is not None
    assert own.json()["days"]
    assert other_visitor.json()["existing_booking"] is None
    assert other_tenant.json()["existing_booking"] is None
    assert "tenant_id" not in own.json()
    assert "visitor_id" not in own.json()


# ---------------------------------------------------------------------------
# SR-6: calendly_handoff action + POST /public/schedule/handoff-intent
# ---------------------------------------------------------------------------


async def test_availability_summary_calendly_configured_returns_handoff_action() -> None:
    db = _StubDatabase()
    db.seed_calendly(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/public/schedule/availability-summary",
            headers={"Authorization": f"Bearer {_visitor_token()}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "calendly_handoff"
    assert data["scheduling_url"] == "https://calendly.com/acme/intro"
    assert data["days"] == []
    assert "tenant_id" not in data
    assert "visitor_id" not in data


async def test_availability_summary_calendly_disabled_falls_through_to_native() -> None:
    db = _StubDatabase()
    db.seed_calendly(tenant_id=_TENANT_ID, enabled=False)
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/public/schedule/availability-summary",
            headers={"Authorization": f"Bearer {_visitor_token()}"},
        )

    assert response.status_code == 200
    assert response.json()["action"] == "schedule_cta"


async def test_availability_summary_calendly_no_scheduling_url_falls_through() -> None:
    """A calendly provider row with no scheduling_url never short-circuits (defensive)."""
    db = _StubDatabase()
    db.seed_calendly(tenant_id=_TENANT_ID, scheduling_url="")
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/public/schedule/availability-summary",
            headers={"Authorization": f"Bearer {_visitor_token()}"},
        )

    assert response.status_code == 200
    assert response.json()["action"] == "schedule_cta"


async def test_availability_summary_native_tenant_unchanged_by_calendly_branch() -> None:
    """A native (non-Calendly) tenant's schedule_cta/lead_form behavior is unchanged."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/public/schedule/availability-summary",
            headers={"Authorization": f"Bearer {_visitor_token()}"},
        )

    assert response.status_code == 200
    assert response.json()["action"] == "schedule_cta"
    assert response.json()["scheduling_url"] is None


async def test_post_handoff_intent_writes_intent_scoped_to_claims() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(tenant_id=_TENANT_ID, visitor_id="visitor-real")
        response = await client.post(
            "/public/schedule/handoff-intent",
            json={"email": "invite@example.com"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"recorded": True}
    assert len(db._handoff_intents) == 1
    stored = db._handoff_intents[0]
    assert stored["tenant_id"] == _TENANT_ID
    assert stored["visitor_id"] == "visitor-real"
    assert stored["email"] == "invite@example.com"


async def test_post_handoff_intent_ignores_body_supplied_ids() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(tenant_id=_TENANT_ID, visitor_id="visitor-real")
        response = await client.post(
            "/public/schedule/handoff-intent",
            json={"email": "invite@example.com", "visitor_id": "visitor-fake", "tenant_id": "tenant-fake"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    stored = db._handoff_intents[0]
    assert stored["visitor_id"] == "visitor-real"
    assert stored["tenant_id"] == _TENANT_ID


# ---------------------------------------------------------------------------
# SR-9.1: booking-lead autolink
# ---------------------------------------------------------------------------


async def test_post_book_no_prior_lead_creates_lead_and_links_and_activity() -> None:
    """No prior lead -> exactly one lead created (source=booking), linked onto
    the event, plus one booked_a_call activity."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    body = _book_body()
    body.update({"email": "qa+booking@example.com", "name": "QA Person"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(visitor_id="visitor-new")
        response = await client.post(
            "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    event_id = response.json()["event_id"]

    assert len(db._leads) == 1
    lead = next(iter(db._leads.values()))
    assert lead["tenant_id"] == _TENANT_ID
    assert lead["source"] == "booking"
    assert lead["email"] == "qa+booking@example.com"
    assert lead["name"] == "QA Person"
    assert lead["stage"] == "captured"
    assert lead["status"] == "new"

    stored_event = db._events[(_TENANT_ID, event_id)]
    assert stored_event["lead_id"] == lead["lead_id"]

    activities = [a for a in db._lead_activities.values() if a["lead_id"] == lead["lead_id"]]
    assert len(activities) == 1
    assert activities[0]["type"] == "booked_a_call"
    assert activities[0]["actor"] == "system"
    assert activities[0]["payload"]["event_id"] == event_id
    assert "starts_at" in activities[0]["payload"]
    assert "timezone" in activities[0]["payload"]


async def test_post_book_with_phone_creates_lead_with_phone() -> None:
    """A booking that includes a phone number persists it on the autolinked
    lead (thread-through of BookRequest.phone -> create_lead(phone=...),
    previously hardcoded to None regardless of what the client sent)."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    body = _book_body()
    body.update({"email": "qa+phone@example.com", "name": "QA Person", "phone": "+1 555-0100"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(visitor_id="visitor-phone")
        response = await client.post(
            "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201

    assert len(db._leads) == 1
    lead = next(iter(db._leads.values()))
    assert lead["phone"] == "+1 555-0100"


async def test_post_book_existing_lead_links_without_duplicating() -> None:
    """A visitor with a prior lead (from POST /public/leads) links onto that
    lead instead of creating a duplicate."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(visitor_id="visitor-existing")
        lead_response = await client.post(
            "/public/leads",
            json={
                "name": "Existing Lead",
                "email": "existing@example.com",
                "consent": {"granted": True, "purpose": "contact", "text": "OK"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert lead_response.status_code == 201
        existing_lead_id = lead_response.json()["lead_id"]

        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    event_id = response.json()["event_id"]

    assert len(db._leads) == 1  # no duplicate
    stored_event = db._events[(_TENANT_ID, event_id)]
    assert stored_event["lead_id"] == existing_lead_id

    activities = [a for a in db._lead_activities.values() if a["lead_id"] == existing_lead_id]
    assert any(a["type"] == "booked_a_call" for a in activities)


async def test_post_book_anonymous_still_creates_lead_with_null_fields() -> None:
    """C4: an anonymous booking (no email/name) still creates a lead, NULL fields."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(visitor_id="visitor-anon")
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    assert len(db._leads) == 1
    lead = next(iter(db._leads.values()))
    assert lead["email"] is None
    assert lead["name"] is None
    assert lead["source"] == "booking"


async def test_post_book_idempotent_rebook_does_not_duplicate_lead() -> None:
    """Two bookings by the same visitor (rebook) never create a second lead."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(visitor_id="visitor-rebook")
        first = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )
        assert first.status_code == 201

        second_body = _book_body(starts_at=f"{_MONDAY}T10:00:00+00:00")
        second = await client.post(
            "/public/schedule/book", json=second_body, headers={"Authorization": f"Bearer {token}"}
        )
        assert second.status_code == 201

    assert len(db._leads) == 1
    lead_id = next(iter(db._leads.values()))["lead_id"]
    first_event = db._events[(_TENANT_ID, first.json()["event_id"])]
    second_event = db._events[(_TENANT_ID, second.json()["event_id"])]
    assert first_event["lead_id"] == lead_id
    assert second_event["lead_id"] == lead_id


async def test_post_book_lead_write_failure_does_not_fail_booking() -> None:
    """C1: a lead-write failure (create_lead/set_event_lead_id raises) never
    fails the booking -- still 201, event persists."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    db.raise_on_lead_write = RuntimeError("db unavailable")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(visitor_id="visitor-fail")
        response = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    event_id = response.json()["event_id"]
    assert (_TENANT_ID, event_id) in db._events
    assert db._events[(_TENANT_ID, event_id)]["status"] == "booked"
    # No lead was created (the raise happened inside create_lead).
    assert db._leads == {}


async def test_post_book_lead_write_failure_logs_degraded_warning(caplog: Any) -> None:
    """C1: on failure, booking_lead_link_degraded is logged (warning), never re-raised."""
    import logging

    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    db.raise_on_lead_write = RuntimeError("db unavailable")

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token(visitor_id="visitor-fail-2")
            response = await client.post(
                "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    assert "booking_lead_link_degraded" in caplog.text


async def test_post_book_lead_tenant_isolation() -> None:
    """MANDATORY: tenant A's booking never creates/links a lead visible to tenant B;
    the same visitor_id under two tenants resolves independently."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    db.seed_availability(tenant_id=_OTHER_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token_a = _visitor_token(tenant_id=_TENANT_ID, visitor_id="visitor-shared")
        token_b = _visitor_token(tenant_id=_OTHER_TENANT_ID, visitor_id="visitor-shared")

        response_a = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token_a}"}
        )
        response_b = await client.post(
            "/public/schedule/book", json=_book_body(), headers={"Authorization": f"Bearer {token_b}"}
        )

    assert response_a.status_code == 201
    assert response_b.status_code == 201

    leads_a = [lead for lead in db._leads.values() if lead["tenant_id"] == _TENANT_ID]
    leads_b = [lead for lead in db._leads.values() if lead["tenant_id"] == _OTHER_TENANT_ID]
    assert len(leads_a) == 1
    assert len(leads_b) == 1
    assert leads_a[0]["lead_id"] != leads_b[0]["lead_id"]

    event_a = db._events[(_TENANT_ID, response_a.json()["event_id"])]
    event_b = db._events[(_OTHER_TENANT_ID, response_b.json()["event_id"])]
    assert event_a["lead_id"] == leads_a[0]["lead_id"]
    assert event_b["lead_id"] == leads_b[0]["lead_id"]


async def test_post_book_created_lead_readable_via_admin_leads_rbac() -> None:
    """MANDATORY RBAC: the booking-created lead is readable by CLIENT_ADMIN/
    CLIENT_AGENT of that tenant; VISITOR cannot list; PLATFORM_ADMIN (global)
    is rejected."""
    from api.auth.tokens import create_access_token

    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        visitor_token = _visitor_token(visitor_id="visitor-rbac")
        booking = await client.post(
            "/public/schedule/book", json=_book_body(),
            headers={"Authorization": f"Bearer {visitor_token}"},
        )
        assert booking.status_code == 201

        admin_token = _admin_token()
        admin_response = await client.get(
            "/admin/leads", cookies={"access_token": admin_token},
        )

        agent_claims = AuthClaims(subject="agent-1", role=Role.CLIENT_AGENT, tenant_id=_TENANT_ID)
        agent_token, _ = create_access_token(agent_claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
        agent_response = await client.get(
            "/admin/leads", cookies={"access_token": agent_token},
        )

        visitor_response = await client.get(
            "/admin/leads", cookies={"access_token": visitor_token},
        )

        global_claims = AuthClaims(subject="platform-1", role=Role.PLATFORM_ADMIN, tenant_id=None)
        global_token, _ = create_access_token(global_claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
        global_response = await client.get(
            "/admin/leads", cookies={"access_token": global_token},
        )

    assert admin_response.status_code == 200
    assert admin_response.json()["total"] == 1
    assert admin_response.json()["items"][0]["source"] == "booking"

    assert agent_response.status_code == 200
    assert agent_response.json()["total"] == 1

    assert visitor_response.status_code == 403

    # PLATFORM_ADMIN is global (no tenant_id) and is not one of the roles
    # accepted by GET /admin/leads (CLIENT_ADMIN/CLIENT_AGENT) -- rejected at
    # the RBAC layer (403), before the repository's own _reject_global would
    # ever run.
    assert global_response.status_code == 403


async def test_post_book_lead_link_never_logs_pii(caplog: Any) -> None:
    """PII: booking_lead_link_degraded and other new log lines carry only
    event_id/tenant_id, never email/name/phone."""
    import logging

    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    db.raise_on_lead_write = RuntimeError("db unavailable")

    secret_email = "super-secret-pii@example.com"
    secret_name = "Super Secret Name"
    body = _book_body()
    body.update({"email": secret_email, "name": secret_name})

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token(visitor_id="visitor-pii")
            response = await client.post(
                "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    assert "booking_lead_link_degraded" in caplog.text
    assert secret_email not in caplog.text
    assert secret_name not in caplog.text


async def test_post_handoff_intent_no_bearer_returns_401() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/public/schedule/handoff-intent", json={"email": "a@example.com"}
        )

    assert response.status_code == 401
    assert db._handoff_intents == []


async def test_post_handoff_intent_invalid_email_returns_422() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token()
        response = await client.post(
            "/public/schedule/handoff-intent",
            json={"email": "not-an-email"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    assert db._handoff_intents == []


# ---------------------------------------------------------------------------
# SR-21: lead_captured feed emit (D2/D3) -- 3rd of 3 mandatory call sites
# ---------------------------------------------------------------------------


async def test_post_book_no_prior_lead_emits_lead_captured() -> None:
    """The new-lead branch of the booking autolink emits exactly one
    lead_captured event."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    body = _book_body()
    body.update({"email": "qa+emit@example.com", "name": "QA Emit"})

    with patch("api.scheduling.routes.emit_event_safe") as mock_emit:
        mock_emit.return_value = "event-1"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token(visitor_id="visitor-emit-1")
            response = await client.post(
                "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "lead_captured"
    assert kwargs["category"] == "leads"
    lead = next(iter(db._leads.values()))
    assert kwargs["target_id"] == lead["lead_id"]
    assert kwargs["payload"] == {"lead_id": lead["lead_id"]}


async def test_post_book_existing_lead_link_does_not_emit_lead_captured() -> None:
    """Linking onto an existing lead (no new lead row) does not emit."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)

    # First booking creates the lead (real create_lead call against the stub).
    body1 = _book_body()
    body1.update({"email": "qa+existing@example.com", "name": "QA Existing"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _visitor_token(visitor_id="visitor-existing-1")
        first = await client.post(
            "/public/schedule/book", json=body1, headers={"Authorization": f"Bearer {token}"}
        )
    assert first.status_code == 201

    # Second booking by the SAME visitor links onto the existing lead --
    # no create_lead, so no lead_captured should be emitted for it.
    body2 = _book_body(starts_at=f"{_MONDAY}T10:00:00+00:00")
    with patch("api.scheduling.routes.emit_event_safe") as mock_emit:
        mock_emit.return_value = "event-2"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token(visitor_id="visitor-existing-1")
            second = await client.post(
                "/public/schedule/book", json=body2, headers={"Authorization": f"Bearer {token}"}
            )

    assert second.status_code == 201
    mock_emit.assert_not_awaited()


async def test_post_book_still_201_when_feed_emit_raises() -> None:
    """MANDATORY (D2): a feed-insert failure must not fail the booking."""
    db = _StubDatabase()
    db.seed_availability(tenant_id=_TENANT_ID)
    app = _build_app(db)
    body = _book_body()
    body.update({"email": "qa+failopen@example.com", "name": "QA FailOpen"})

    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _visitor_token(visitor_id="visitor-emit-failopen")
            response = await client.post(
                "/public/schedule/book", json=body, headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    assert len(db._leads) == 1
