"""Plan 2.3 on a REAL migrated Postgres: the housekeeping sweep completes finished sequences
and starts a nurture plan timed to the start date the lead gave. System-wide sweep, every
plan handled under its own tenant."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.agents import nurture, planner
from api.agents.repository import AgentSettings, add_consent, upsert_agent_settings
from api.contacts.repository import add_identity, create_contact, set_contact_timezone
from api.qualification import repository as qrepo

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)


@pytest.fixture
async def env() -> Any:
    assert _TEST_DB_URL is not None
    db = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0)
    tenants = []
    for _ in range(2):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, $1, $1, true, $1)", tid)
        claims = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tid)
        await upsert_agent_settings(db, claims, AgentSettings(
            enabled_channels=("email",), physical_address="1 Main St",
            min_hours_between_touches=1))
        tenants.append(claims)
    yield {"db": db, "a": tenants[0], "b": tenants[1]}
    await db.close()


async def _finished(db: Database, claims: AuthClaims, *, start: str | None,
                    closed_ago: timedelta = timedelta(days=2),
                    leave_open: bool = False) -> tuple[str, str]:
    """A contact whose new-enquiry sequence has run out (its tasks all done ``closed_ago``),
    with ``start`` as their answer to "When would you like to get started?"."""
    email = f"{uuid4().hex[:8]}@example.com"
    cid = await create_contact(db, claims, account_id=None, lead_id=None, name="Jane Doe",
                               email=email, phone=None, consent={})
    await add_identity(db, claims, cid, identity_type="email", identity_value=email)
    await set_contact_timezone(db, claims, cid, "America/New_York")
    await add_consent(db, claims, cid, channel="email", status="granted",
                      basis="express_consent", source="form")
    if start is not None:
        await qrepo.save_answer(db, claims, cid, "start", choice=start, heard=start,
                                channel="call", at=datetime.now(UTC))
    out = await planner.create_plan(db, claims, contact_id=cid, kind="inbound_auto",
                                    goal="follow_up", now=datetime.now(UTC) - timedelta(days=9))
    assert out.plan_id
    await db.execute("UPDATE outreach_tasks SET status = 'completed', completed_at = $2 "
                     "WHERE plan_id = $1", out.plan_id, datetime.now(UTC) - closed_ago)
    if leave_open:
        await db.execute("UPDATE outreach_tasks SET status = 'scheduled' WHERE plan_id = $1 "
                         "AND step_index = (SELECT max(step_index) FROM outreach_tasks "
                         "WHERE plan_id = $1)", out.plan_id)
    return cid, out.plan_id


async def _plans(db: Database, contact_id: str) -> list[tuple[str, str]]:
    return [(r["kind"], r["status"]) for r in await db.fetch(
        "SELECT plan->>'kind' AS kind, status FROM outreach_plans WHERE contact_id = $1 "
        "ORDER BY created_at", contact_id)]


async def _nurture_offsets(db: Database, contact_id: str) -> list[int]:
    """Days from now to each nurture email, rounded."""
    rows = await db.fetch(
        "SELECT t.scheduled_at FROM outreach_tasks t JOIN outreach_plans p USING (tenant_id, "
        "plan_id) WHERE p.contact_id = $1 AND p.plan->>'kind' = 'nurture' ORDER BY step_index",
        contact_id)
    now = datetime.now(UTC)
    return [round((r["scheduled_at"] - now).total_seconds() / 86400) for r in rows]


async def test_later_gets_a_check_in_in_about_three_months(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    cid, _ = await _finished(db, a, start="later")
    await nurture.sweep_system(db, now=datetime.now(UTC))
    assert await _plans(db, cid) == [("inbound_auto", "completed"), ("nurture", "active")]
    assert await _nurture_offsets(db, cid) == [90, 120]


async def test_in_one_to_three_months_gets_one_in_about_a_month(env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    cid, _ = await _finished(db, a, start="in 1 to 3 months")
    await nurture.sweep_system(db, now=datetime.now(UTC))
    assert await _nurture_offsets(db, cid) == [30, 60]


async def test_ready_or_unknown_timelines_are_completed_without_nurture(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    ready, _ = await _finished(db, a, start="this month")
    unknown, _ = await _finished(db, a, start=None)
    await nurture.sweep_system(db, now=datetime.now(UTC))
    assert await _plans(db, ready) == [("inbound_auto", "completed")]
    assert await _plans(db, unknown) == [("inbound_auto", "completed")]


async def test_a_sequence_still_running_or_just_finished_is_left_alone(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    running, _ = await _finished(db, a, start="later", leave_open=True)
    fresh, _ = await _finished(db, a, start="later", closed_ago=timedelta(hours=3))
    await nurture.sweep_system(db, now=datetime.now(UTC))
    assert await _plans(db, running) == [("inbound_auto", "active")]
    assert await _plans(db, fresh) == [("inbound_auto", "active")]


async def test_someone_with_a_booking_coming_up_is_not_nurtured(env: dict[str, Any]) -> None:
    from api.scheduling.repository import create_event

    db, a = env["db"], env["a"]
    cid, _ = await _finished(db, a, start="later")
    email = await db.fetchval("SELECT email FROM contacts WHERE contact_id = $1", cid)
    soon = datetime.now(UTC) + timedelta(days=2)
    await create_event(db, a, starts_at=soon, ends_at=soon + timedelta(minutes=30),
                       timezone="America/New_York", visitor_id=None, lead_id=None, consent={},
                       email=email, name="Jane")
    await nurture.sweep_system(db, now=datetime.now(UTC))
    assert await _plans(db, cid) == [("inbound_auto", "completed")]


async def test_each_plan_is_handled_under_its_own_tenant(env: dict[str, Any]) -> None:
    db, a, b = env["db"], env["a"], env["b"]
    in_a, _ = await _finished(db, a, start="later")
    in_b, _ = await _finished(db, b, start="this month")
    await nurture.sweep_system(db, now=datetime.now(UTC))
    assert await _plans(db, in_a) == [("inbound_auto", "completed"), ("nurture", "active")]
    assert await _plans(db, in_b) == [("inbound_auto", "completed")]
    assert await db.fetchval("SELECT tenant_id FROM outreach_plans WHERE contact_id = $1 AND "
                             "plan->>'kind' = 'nurture'", in_a) == a.tenant_id
