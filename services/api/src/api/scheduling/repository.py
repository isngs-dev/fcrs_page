"""Scheduling repository — tenant-scoped async SQL for availability + booking.

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …).
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

Data model (migration 0018):
- ``availability(tenant_id PK, timezone, rules jsonb, updated_at)`` — one row
  per tenant, upserted via ``ON CONFLICT``.
- ``schedule_events(tenant_id, event_id, lead_id, visitor_id, starts_at,
  ends_at, timezone, status, calendar_ref, consent jsonb, created_at)`` —
  composite PK ``(tenant_id, event_id)``. A partial unique index on
  ``(tenant_id, starts_at) WHERE status = 'booked'`` is the DB-enforced
  no-double-booking guard (S8.1 decision 4); ``create_event`` catches the
  resulting ``asyncpg.UniqueViolationError`` and raises ``ValidationError``
  code ``SLOT_UNAVAILABLE``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import asyncpg
from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


@dataclass(frozen=True)
class Availability:
    """A tenant's availability rules + timezone."""

    timezone: str
    rules: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class EventContact:
    """A single event's contact-resolution fields (S9.2, Scope §6).

    Used by ``api.notifications.recipients.resolve_event_recipient`` --
    intentionally narrow (no consent/calendar_ref) since it exists only to
    resolve an outbound recipient. ``meet_url`` (SR-22) IS included, unlike
    ``calendar_ref``/``consent`` -- both the booking-confirmation and
    3d/24h/1h reminder emails need it to give the visitor a working join
    link, which is exactly what this struct exists to carry to those
    templates (``api.notifications.templates``).
    """

    lead_id: str | None
    visitor_id: str | None
    timezone: str
    starts_at: datetime
    status: str
    email: str | None = None
    meet_url: str | None = None


@dataclass(frozen=True)
class ScheduleEvent:
    """A single booked/cancelled/completed/no-show call."""

    event_id: str
    lead_id: str | None
    visitor_id: str | None
    email: str | None
    name: str | None
    starts_at: datetime
    ends_at: datetime
    timezone: str
    status: str
    calendar_ref: str | None
    consent: dict[str, Any]
    created_at: datetime
    source: str = "native"
    # SR-22. Defaults None for every construction site that doesn't SELECT
    # it (a fresh booking never has one yet; list_events_for_timeline/
    # get_upcoming_booking don't select it -- deliberately out of scope for
    # this change, see the migration's docstring) -- never fabricated.
    meet_url: str | None = None


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Scheduling is always tenant-scoped; a global caller has no tenant_id and
    therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Scheduling repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def get_availability(db: Database, claims: AuthClaims) -> Availability | None:
    """Fetch the caller's tenant availability, or ``None`` if unset."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT timezone, rules, updated_at FROM availability WHERE tenant_id = $1",
        claims.tenant_id,
    )
    return _row_to_availability(row) if row is not None else None


async def upsert_availability(
    db: Database,
    claims: AuthClaims,
    *,
    timezone: str,
    rules: dict[str, Any],
) -> Availability:
    """Insert or update the caller's tenant availability. Returns the stored row.

    ``rules`` is bound as jsonb (the default codec handles dict<->jsonb).
    """
    _reject_global(claims)

    await db.execute(
        "INSERT INTO availability (tenant_id, timezone, rules, updated_at) "
        "VALUES ($1, $2, $3, now()) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "timezone = EXCLUDED.timezone, rules = EXCLUDED.rules, updated_at = now()",
        claims.tenant_id,
        timezone,
        rules,
    )

    result = await get_availability(db, claims)
    assert result is not None  # noqa: S101  # we just wrote the row
    return result


async def list_booked(
    db: Database,
    claims: AuthClaims,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Fetch ``(starts_at, ends_at)`` for the tenant's ``status='booked'``
    events whose ``starts_at`` falls within ``[window_start, window_end]``.
    """
    _reject_global(claims)

    rows = await db.fetch(
        "SELECT starts_at, ends_at FROM schedule_events "
        "WHERE tenant_id = $1 AND status = 'booked' "
        "AND starts_at >= $2 AND starts_at <= $3 "
        "ORDER BY starts_at",
        claims.tenant_id,
        window_start,
        window_end,
    )
    return [(row["starts_at"], row["ends_at"]) for row in rows]


async def create_event(
    db: Database,
    claims: AuthClaims,
    *,
    starts_at: datetime,
    ends_at: datetime,
    timezone: str,
    visitor_id: str | None,
    lead_id: str | None,
    consent: dict[str, Any],
    email: str | None = None,
    name: str | None = None,
) -> ScheduleEvent:
    """Insert a new ``schedule_events`` row with ``status='booked'``.

    Returns the created ``ScheduleEvent`` (``event_id = uuid4().hex``,
    ``calendar_ref = None`` — S8.2 wires calendar sync). If the partial
    unique index ``schedule_events_no_double_book`` rejects the insert
    (``asyncpg.UniqueViolationError``, i.e. the tenant already has a booked
    event at this ``starts_at``), raises ``ValidationError`` code
    ``SLOT_UNAVAILABLE`` — nothing is persisted.
    """
    _reject_global(claims)

    new_event_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO schedule_events "
            "(tenant_id, event_id, lead_id, visitor_id, email, name, starts_at, ends_at, "
            " timezone, status, calendar_ref, consent, source) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
            claims.tenant_id,
            new_event_id,
            lead_id,
            visitor_id,
            email,
            name,
            starts_at,
            ends_at,
            timezone,
            "booked",
            None,  # calendar_ref (S8.2)
            consent,
            "native",  # source (SR-6 decision 7) -- explicit, never implied
        )
    except asyncpg.UniqueViolationError as exc:
        raise ValidationError(
            "The requested time is no longer available.",
            code="SLOT_UNAVAILABLE",
        ) from exc

    # The DB stamps created_at server-side via DEFAULT now(); populate the
    # returned object without a round-trip SELECT.
    return ScheduleEvent(
        event_id=new_event_id,
        lead_id=lead_id,
        visitor_id=visitor_id,
        email=email,
        name=name,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=timezone,
        status="booked",
        calendar_ref=None,
        consent=consent,
        created_at=datetime.now(UTC),
        source="native",
    )


async def update_event_calendar_ref(
    db: Database,
    claims: AuthClaims,
    event_id: str,
    calendar_ref: str,
    meet_url: str | None = None,
) -> None:
    """Persist a ``CalendarProvider.create_event`` result onto a booked event.

    Called after a successful calendar sync (S8.2 decision 4). ``meet_url``
    (SR-22) defaults ``None`` -- unchanged behavior for any provider/config
    that doesn't create a Google Meet conference (``StubCalendarProvider``
    with no fake link, a future provider that doesn't support conferences).
    Tenant-scoped -- the ``WHERE`` clause filters by ``tenant_id`` so this
    can never touch another tenant's row even if ``event_id`` were guessed.
    """
    _reject_global(claims)

    await db.execute(
        "UPDATE schedule_events SET calendar_ref = $3, meet_url = $4 "
        "WHERE tenant_id = $1 AND event_id = $2",
        claims.tenant_id,
        event_id,
        calendar_ref,
        meet_url,
    )


async def set_event_lead_id(
    db: Database,
    claims: AuthClaims,
    event_id: str,
    lead_id: str,
) -> None:
    """Link a resolved (created-or-existing) lead onto a booked event.

    Called by ``api.scheduling.routes.book_slot`` (SR-9.1) as part of the
    best-effort, post-durable-write lead autolink -- never inside the same
    transaction as ``create_event``. Tenant-scoped: the ``WHERE`` clause
    filters by ``tenant_id`` so this can never touch another tenant's row.
    """
    _reject_global(claims)

    await db.execute(
        "UPDATE schedule_events SET lead_id = $1 "
        "WHERE tenant_id = $2 AND event_id = $3",
        lead_id,
        claims.tenant_id,
        event_id,
    )


async def delete_event(db: Database, claims: AuthClaims, event_id: str) -> None:
    """Delete a ``schedule_events`` row.

    Compensation for a calendar sync failure right after ``create_event``
    (S8.2 decision 4) -- never leaves an orphaned booked row without its
    calendar event when a calendar is enabled. Tenant-scoped.
    """
    _reject_global(claims)

    await db.execute(
        "DELETE FROM schedule_events WHERE tenant_id = $1 AND event_id = $2",
        claims.tenant_id,
        event_id,
    )


async def get_event_contact(
    db: Database, claims: AuthClaims, event_id: str
) -> EventContact | None:
    """Fetch a single event's contact-resolution fields, tenant-scoped.

    Used by ``api.notifications.recipients.resolve_event_recipient`` (S9.2,
    Decision 3) -- never exposes ``calendar_ref``/``consent``. Returns
    ``None`` if the event is missing or belongs to another tenant.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT lead_id, visitor_id, email, timezone, starts_at, status, meet_url "
        "FROM schedule_events WHERE tenant_id = $1 AND event_id = $2",
        claims.tenant_id,
        event_id,
    )
    if row is None:
        return None
    return EventContact(
        lead_id=row["lead_id"],
        visitor_id=row["visitor_id"],
        timezone=str(row["timezone"]),
        starts_at=row["starts_at"],
        status=str(row["status"]),
        email=row["email"],
        meet_url=row["meet_url"],
    )


async def list_events_for_timeline(
    db: Database,
    claims: AuthClaims,
    *,
    lead_ids: list[str],
    visitor_ids: list[str],
    before: datetime | None = None,
    limit: int = 50,
) -> list[ScheduleEvent]:
    """Fetch tenant-scoped events reachable by either identity path (SR-9.3 J8).

    ``AND (lead_id = ANY($2) OR visitor_id = ANY($3))`` -- a Calendly-sourced
    event never has a ``lead_id`` (see ``ingest_calendly_event``) but does
    carry a ``visitor_id`` from email correlation, so the OR is required or
    Calendly bookings silently vanish from the timeline. Both lists may be
    empty (an empty ``ANY($n)`` array matches nothing, which is correct --
    not an error). Newest-first by ``starts_at``. Raises ``ValidationError``
    for global callers.
    """
    _reject_global(claims)

    params: list[Any] = [claims.tenant_id, lead_ids, visitor_ids]
    where = "WHERE tenant_id = $1 AND (lead_id = ANY($2) OR visitor_id = ANY($3))"

    if before is not None:
        params.append(before)
        where += f" AND starts_at < ${len(params)}"

    params.append(max(1, min(limit, 200)))

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    rows = await db.fetch(
        "SELECT event_id, lead_id, visitor_id, email, name, starts_at, ends_at, "
        "timezone, status, calendar_ref, consent, created_at, source "
        "FROM schedule_events " + where + " "
        f"ORDER BY starts_at DESC LIMIT ${len(params)}",
        *params,
    )
    return [
        ScheduleEvent(
            event_id=str(row["event_id"]), lead_id=row["lead_id"], visitor_id=row["visitor_id"],
            email=row["email"], name=row["name"], starts_at=row["starts_at"],
            ends_at=row["ends_at"], timezone=str(row["timezone"]), status=str(row["status"]),
            calendar_ref=row["calendar_ref"], consent=row["consent"], created_at=row["created_at"],
            source=str(row["source"]) if "source" in row.keys() and row["source"] is not None
            else "native",
        )
        for row in rows
    ]


async def get_upcoming_booking(
    db: Database, claims: AuthClaims, visitor_id: str
) -> ScheduleEvent | None:
    """Return the caller visitor's soonest future booked event, if any.

    Both tenant and visitor predicates are required: a visitor identifier is
    not globally unique and must never be queried without the tenant boundary.
    """
    _reject_global(claims)
    now = datetime.now(UTC)
    row = await db.fetchrow(
        "SELECT event_id, lead_id, visitor_id, email, name, starts_at, ends_at, "
        "timezone, status, calendar_ref, consent, created_at FROM schedule_events "
        "WHERE tenant_id = $1 AND visitor_id = $2 AND status = 'booked' "
        "AND starts_at > $3 ORDER BY starts_at LIMIT 1",
        claims.tenant_id,
        visitor_id,
        now,
    )
    if row is None:
        return None
    return ScheduleEvent(
        event_id=str(row["event_id"]), lead_id=row["lead_id"], visitor_id=row["visitor_id"],
        email=row["email"], name=row["name"], starts_at=row["starts_at"], ends_at=row["ends_at"],
        timezone=str(row["timezone"]), status=str(row["status"]), calendar_ref=row["calendar_ref"],
        consent=row["consent"], created_at=row["created_at"],
    )


async def ingest_calendly_event(
    db: Database,
    tenant_id: str,
    *,
    calendly_uuid: str,
    starts_at: datetime,
    ends_at: datetime,
    timezone: str,
    email: str | None,
    name: str | None,
    visitor_id: str | None,
) -> ScheduleEvent:
    """Idempotently UPSERT a Calendly ``invitee.created`` event (SR-6, Scope §8).

    Claims-less (called ONLY from the signature-verified Calendly webhook,
    which has no session) -- ``tenant_id`` comes from the webhook's already-
    verified path parameter, never from the payload. ``calendar_ref`` is
    ``f"calendly:{calendly_uuid}"`` (mirrors the existing
    ``f"{provider}:{external_id}"`` text convention from ``book_slot``'s
    native calendar sync) -- this is the DB-level idempotency key: the
    partial unique index ``schedule_events_calendly_idempotent`` on
    ``(tenant_id, calendar_ref) WHERE source = 'calendly'`` (migration 0034)
    makes ``ON CONFLICT`` UPDATE the existing row on a re-delivered event
    rather than double-inserting (SR-6 decision 6a) -- never an app-level
    SELECT-then-INSERT race. ``visitor_id`` is the 5a email-correlation
    result, or ``None`` on honest no-match (decision 5b) -- never fabricated.
    ``consent`` records provenance only (decision 6b) -- we do not fabricate
    our own consent object; Calendly's own flow captured it.
    """
    new_event_id = uuid4().hex
    calendar_ref = f"calendly:{calendly_uuid}"
    consent = {
        "granted": True,
        "purpose": "calendly_booking",
        "text": "Consent captured by Calendly's own booking flow.",
        "provenance": "calendly",
    }

    row = await db.fetchrow(
        "INSERT INTO schedule_events "
        "(tenant_id, event_id, lead_id, visitor_id, email, name, starts_at, ends_at, "
        " timezone, status, calendar_ref, consent, source) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
        "ON CONFLICT (tenant_id, calendar_ref) WHERE source = 'calendly' DO UPDATE SET "
        "starts_at = EXCLUDED.starts_at, ends_at = EXCLUDED.ends_at, "
        "timezone = EXCLUDED.timezone, email = EXCLUDED.email, name = EXCLUDED.name, "
        "status = 'booked', visitor_id = COALESCE(schedule_events.visitor_id, EXCLUDED.visitor_id) "
        "RETURNING event_id, lead_id, visitor_id, email, name, starts_at, ends_at, "
        "timezone, status, calendar_ref, consent, created_at, source",
        tenant_id,
        new_event_id,
        None,  # lead_id -- Calendly bookings never create a CRM lead (decision, "Isn't" list)
        visitor_id,
        email,
        name,
        starts_at,
        ends_at,
        timezone,
        "booked",
        calendar_ref,
        consent,
        "calendly",
    )
    assert row is not None  # noqa: S101  # INSERT ... ON CONFLICT ... RETURNING always returns a row

    return ScheduleEvent(
        event_id=str(row["event_id"]), lead_id=row["lead_id"], visitor_id=row["visitor_id"],
        email=row["email"], name=row["name"], starts_at=row["starts_at"], ends_at=row["ends_at"],
        timezone=str(row["timezone"]), status=str(row["status"]), calendar_ref=row["calendar_ref"],
        consent=row["consent"], created_at=row["created_at"], source=str(row["source"]),
    )


async def cancel_calendly_event(db: Database, tenant_id: str, calendly_uuid: str) -> None:
    """Idempotent ``status='cancelled'`` flip for a Calendly ``invitee.canceled`` event.

    Claims-less (webhook-only, see ``ingest_calendly_event`` docstring).
    Tenant-scoped by the already-verified path ``tenant_id``. A cancel for an
    unknown/never-ingested Calendly UUID is a no-op success -- never an
    error, never a fabricated row (SR-6 decision 6c).
    """
    await db.execute(
        "UPDATE schedule_events SET status = 'cancelled' "
        "WHERE tenant_id = $1 AND calendar_ref = $2 AND source = 'calendly'",
        tenant_id,
        f"calendly:{calendly_uuid}",
    )


def _row_to_availability(row: Any) -> Availability:
    return Availability(
        timezone=str(row["timezone"]),
        rules=row["rules"],
        updated_at=row["updated_at"],
    )
