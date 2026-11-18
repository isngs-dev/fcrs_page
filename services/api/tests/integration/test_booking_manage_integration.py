"""Plan 1.6 on a REAL migrated Postgres over HTTP: the booking confirmation's agenda, prep
question and manage link, and the public manage page -- look (changes nothing), move to an
open time (new booking first, then the old one retired), cancel. The token is the only
credential: forged or another tenant's booking -> 404."""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from httpx import ASGITransport, AsyncClient

from api.agents import memory
from api.contacts.repository import create_contact
from api.scheduling import booking
from api.scheduling.repository import upsert_availability

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)

_ENV = {
    "DEPLOYMENT_MODE": "saas", "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379", "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api", "LOG_LEVEL": "WARNING", "COOKIE_SECURE": "false",
    "EMAIL_LINK_SECRET": "l" * 40, "PUBLIC_API_BASE_URL": "https://api.example.com",
}


class _Redis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    assert _TEST_DB_URL is not None
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_api_settings.cache_clear()
    db = await Database.connect(_TEST_DB_URL, min_size=1, max_size=5, statement_cache_size=0)
    tenants = []
    for _ in range(2):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, 'Acme Roofing Marketing', $1, true, $1)", tid)
        claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tid)
        await upsert_availability(db, claims, timezone="America/New_York", rules={
            "slot_minutes": 30, "buffer_minutes": 0,
            "weekly_hours": {d: [["00:00", "23:30"]] for d in
                             ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}})
        tenants.append(claims)
    notify = MagicMock()
    monkeypatch.setattr(booking, "send_notification", notify)
    from api.app import create_app

    app = create_app()
    app.state.db, app.state.redis, app.state.cache = db, _Redis(), InMemoryCache()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"db": db, "client": client, "a": tenants[0], "b": tenants[1], "notify": notify}
    await db.close()
    get_settings.cache_clear()
    get_api_settings.cache_clear()


async def _slots(db: Database, claims: AuthClaims) -> list[datetime]:
    found = await booking.next_open_slots(db, claims, datetime.now(UTC), count=None)
    assert found is not None
    return found[0]


async def _booked(env: dict[str, Any], claims: AuthClaims, *, with_notes: bool = True
                  ) -> tuple[Any, str]:
    """A contact who filled in the form, booked into the first open slot, confirmed."""
    db = env["db"]
    cid = await create_contact(db, claims, account_id=None, lead_id=None, name="Jane Doe",
                               email="jane@example.com", phone="+12025550123", consent={})
    if with_notes:
        for text in ("service: seo", "message: We want more roof replacement jobs",
                     "company: Lee Roofing"):
            await memory.remember(db, claims, cid, channel="website", kind="need", text=text)
    start = (await _slots(db, claims))[0]
    status, event, meet, organizer = await booking.book_slot(
        db, claims, start=start, timezone="America/New_York", email="jane@example.com",
        name="Jane Doe", lead_id=None, visitor_id=None, attendee_phone=None,
        consent={"granted": True, "purpose": "booking", "text": "test"})
    assert status == "booked" and event is not None
    await booking.send_confirmation(db, claims, event, recipient="jane@example.com",
                                    name="Jane Doe", business="Acme Roofing", meet_url=meet,
                                    organizer=organizer, contact_id=cid)
    return event, cid


async def _confirmations(db: Database, claims: AuthClaims) -> list[str]:
    return [r["body"] for r in await db.fetch(
        "SELECT body FROM notification_jobs WHERE tenant_id = $1 AND "
        "payload->>'kind' = 'booking_confirm' ORDER BY created_at", claims.tenant_id)]


def _token(body: str) -> str:
    match = re.search(r"https://api\.example\.com/public/bookings/(\S+)", body)
    assert match, body
    return match.group(1)


async def test_the_confirmation_has_their_agenda_a_prep_question_and_a_manage_link(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    await _booked(env, a)
    [body] = await _confirmations(db, a)
    assert "What we'll cover:\n- SEO\n- We want more roof replacement jobs" in body
    assert "Lee Roofing" not in body  # the company is a detail, not a topic
    assert ("To help us prepare, could you reply with: Roughly how many roofing jobs do you "
            "do a month?") in body
    assert "Need to move or cancel it? Manage your booking here: " in body
    assert env["notify"].delay.call_count == 1


async def test_without_notes_or_open_questions_nothing_empty_is_shown(
        env: dict[str, Any]) -> None:
    from api.qualification import repository as qrepo
    from api.qualification.scoring import QualConfig

    db, a = env["db"], env["a"]
    await qrepo.save_config(db, a, QualConfig(questions=[]))
    await _booked(env, a, with_notes=False)
    [body] = await _confirmations(db, a)
    assert "What we'll cover" not in body and "help us prepare" not in body


async def test_the_page_shows_the_booking_and_open_times_and_changes_nothing(
        env: dict[str, Any]) -> None:
    db, a, client = env["db"], env["a"], env["client"]
    event, _ = await _booked(env, a)
    token = _token((await _confirmations(db, a))[0])
    page = await client.get(f"/public/bookings/{token}")
    assert page.status_code == 200
    assert "Your call with Acme Roofing Marketing" in page.text
    assert "Cancel my call" in page.text and "Move my call" in page.text
    assert page.text.count("name='starts_at'") >= 3  # open times to move to
    assert await db.fetchval("SELECT status FROM schedule_events WHERE event_id = $1",
                             event.event_id) == "booked"  # a mail scanner's GET is harmless


async def test_moving_books_the_new_time_then_retires_the_old_one(env: dict[str, Any]) -> None:
    db, a, client = env["db"], env["a"], env["client"]
    event, _ = await _booked(env, a)
    token = _token((await _confirmations(db, a))[0])
    new_start = (await _slots(db, a))[2]
    moved = await client.post(f"/public/bookings/{token}/move",
                              data={"starts_at": new_start.isoformat()})
    assert moved.status_code == 200 and "Your call is moved" in moved.text
    rows = {r["event_id"]: (r["status"], r["starts_at"]) for r in await db.fetch(
        "SELECT event_id, status, starts_at FROM schedule_events WHERE tenant_id = $1",
        a.tenant_id)}
    assert rows[event.event_id][0] == "cancelled"
    [(new_id, (status, starts))] = [(k, v) for k, v in rows.items() if k != event.event_id]
    assert status == "booked" and starts == new_start
    assert await db.fetchval("SELECT count(*) FROM reminder_jobs WHERE event_id = $1 "
                             "AND status = 'pending'", event.event_id) == 0
    bodies = await _confirmations(db, a)
    assert len(bodies) == 2 and "This replaces your earlier booking" in bodies[1]
    assert _token(bodies[1]) != token  # the new booking has its own link
    old = await client.get(f"/public/bookings/{token}")
    assert "no longer booked" in old.text
    assert await db.fetchval("SELECT count(*) FROM audit_events WHERE tenant_id = $1 "
                             "AND action = 'scheduling.booking_moved'", a.tenant_id) == 1
    assert new_id


async def test_a_time_that_is_no_longer_open_changes_nothing(env: dict[str, Any]) -> None:
    db, a, client = env["db"], env["a"], env["client"]
    event, _ = await _booked(env, a)
    token = _token((await _confirmations(db, a))[0])
    gone = await client.post(f"/public/bookings/{token}/move",
                             data={"starts_at": event.starts_at.isoformat()})  # its own slot
    assert "isn't open any more" in gone.text
    bad = await client.post(f"/public/bookings/{token}/move", data={"starts_at": "soon"})
    assert "isn't open any more" in bad.text
    assert await db.fetchval("SELECT count(*) FROM schedule_events WHERE tenant_id = $1 "
                             "AND status = 'booked'", a.tenant_id) == 1


async def test_cancelling_retires_the_booking_and_tells_the_team(env: dict[str, Any]) -> None:
    db, a, client = env["db"], env["a"], env["client"]
    event, _ = await _booked(env, a)
    token = _token((await _confirmations(db, a))[0])
    done = await client.post(f"/public/bookings/{token}/cancel")
    assert done.status_code == 200 and "Your call is cancelled" in done.text
    assert await db.fetchval("SELECT status FROM schedule_events WHERE event_id = $1",
                             event.event_id) == "cancelled"
    assert await db.fetchval("SELECT count(*) FROM notification_events WHERE tenant_id = $1 "
                             "AND kind = 'booking_cancelled_by_attendee'", a.tenant_id) == 1
    again = await client.post(f"/public/bookings/{token}/cancel")  # idempotent
    assert "no longer booked" in again.text


async def test_forged_or_another_tenants_token_is_not_found(env: dict[str, Any]) -> None:
    from api.scheduling import manage

    db, a, b, client = env["db"], env["a"], env["b"], env["client"]
    event, _ = await _booked(env, a)
    assert (await client.get("/public/bookings/nonsense")).status_code == 404
    cross = manage.sign(str(b.tenant_id), event.event_id)  # A's booking, B's tenant
    assert (await client.get(f"/public/bookings/{cross}")).status_code == 404
    assert (await client.post(f"/public/bookings/{cross}/cancel")).status_code == 404
    assert await db.fetchval("SELECT status FROM schedule_events WHERE event_id = $1",
                             event.event_id) == "booked"
