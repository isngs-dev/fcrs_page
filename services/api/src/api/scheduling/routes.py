"""Visitor-authenticated scheduling routes -- open slots + native booking.

``GET /public/schedule/slots`` is read-only: it returns the tenant's open
slots (availability rules minus already-booked events), or ``[]`` if no
availability is configured (S8.1 decision 6 -- no silent fallback, but an
empty/unconfigured tenant is not an error).

``POST /public/schedule/book`` is consent-gated (GDPR) and re-validates the
requested start is an actually-open slot immediately before inserting, on top
of the DB-enforced partial-unique-index no-double-booking guard
(``api.scheduling.repository.create_event``). ``tenant_id``/``visitor_id``
always come from the visitor session (``get_visitor_claims``), never the
request body. The response is leak-free -- no ``tenant_id``/``visitor_id``.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from common.auth import AuthClaims
from common.errors import ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, field_validator

from api.config import ApiSettings, get_api_settings
from api.gateway.dependencies import get_visitor_claims
from api.leads.assignment import assign_lead_fail_open
from api.leads.repository import add_activity, create_lead, get_lead_id_by_visitor_id
from api.leads.tasks import classify_lead_email
from api.notifications.emit import emit_event_safe
from api.notifications.recipients import resolve_event_recipient
from api.notifications.repository import enqueue_notification
from api.notifications.tasks import send_notification
from api.notifications.templates import booking_confirmation_message
from api.scheduling.calendar import CalendarEvent, calendar_provider_for_async
from api.scheduling.calendar_config_repository import get_calendar_config
from api.scheduling.handoff_intent_repository import create_handoff_intent
from api.scheduling.reminder_repository import create_reminder_jobs
from api.scheduling.repository import (
    Availability,
    create_event,
    delete_event,
    get_availability,
    get_upcoming_booking,
    list_booked,
    set_event_lead_id,
    update_event_calendar_ref,
)
from api.scheduling.slots import Slot, compute_slots

_log = get_logger(__name__)

_SCHEDULE_TRANSITION_MESSAGE = (
    "Happy to connect you with a sales rep. Please pick a time that works best for you."
)

router = APIRouter(prefix="/public/schedule", tags=["scheduling"])


class SlotResponse(BaseModel):
    """A single open slot, UTC."""

    starts_at: datetime
    ends_at: datetime


class ConsentPayload(BaseModel):
    """Consent metadata provided by the visitor."""

    granted: bool
    purpose: str
    text: str


class BookRequest(BaseModel):
    """Body for POST /public/schedule/book."""

    starts_at: datetime
    timezone: str
    consent: ConsentPayload | None = None
    lead_id: str | None = None
    email: str | None = None
    name: str | None = None
    phone: str | None = None

    @field_validator("starts_at")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("starts_at must be timezone-aware (UTC ISO 8601)")
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"invalid IANA timezone: {v}") from exc
        return v

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str | None) -> str | None:
        if v is not None and ("@" not in v or v.startswith("@") or v.endswith("@")):
            raise ValueError("invalid email address")
        return v


class BookResponse(BaseModel):
    """Leak-free (no tenant_id/visitor_id) response for POST /public/schedule/book."""

    event_id: str
    starts_at: datetime
    ends_at: datetime
    status: str


class AvailabilityDayResponse(BaseModel):
    date: date
    has_availability: bool


class ExistingBookingResponse(BaseModel):
    starts_at: datetime
    ends_at: datetime
    timezone: str


class AvailabilitySummaryResponse(BaseModel):
    action: str
    timezone: str
    days: list[AvailabilityDayResponse]
    transition_message: str
    existing_booking: ExistingBookingResponse | None
    # SR-6: the tenant's Calendly hosted-scheduling page, present only when
    # action="calendly_handoff". Leak-free (no tenant_id/visitor_id).
    scheduling_url: str | None = None


class HandoffIntentRequest(BaseModel):
    """Body for POST /public/schedule/handoff-intent (SR-6)."""

    email: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("invalid email address")
        return v


class HandoffIntentResponse(BaseModel):
    """Leak-free (no tenant_id/visitor_id) response for POST /public/schedule/handoff-intent."""

    recorded: bool


def _resolve_window(
    settings: ApiSettings, date_from: date | None, date_to: date | None
) -> tuple[date, date]:
    """Resolve the query window, defaulting per S8.1 decision 2/6."""
    today = datetime.now(UTC).date()
    start = date_from or today
    end = date_to or (start + timedelta(days=settings.schedule_slot_window_days - 1))
    return start, end


def _day_bounds_utc(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC bounds for the local calendar day ``day`` in ``zone``."""
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)
    end = (datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=1)).astimezone(UTC)
    return start, end


async def _open_slots_for_window(
    db: object,
    claims: AuthClaims,
    availability: Availability,
    date_from: date,
    date_to: date,
    *,
    window_max_days: int,
    extra_busy: list[tuple[datetime, datetime]] | None = None,
) -> list[Slot]:
    zone = ZoneInfo(availability.timezone)
    window_start_utc, _ = _day_bounds_utc(date_from, zone)
    _, window_end_utc = _day_bounds_utc(date_to, zone)

    booked = await list_booked(
        db,  # type: ignore[arg-type]
        claims,
        window_start=window_start_utc,
        window_end=window_end_utc,
    )
    if extra_busy:
        booked = booked + extra_busy

    return compute_slots(
        availability.rules,
        availability.timezone,
        date_from,
        date_to,
        booked,
        now=datetime.now(UTC),
        window_max_days=window_max_days,
    )


async def _calendar_busy_for_window(
    db: object,
    claims: AuthClaims,
    window_start: datetime,
    window_end: datetime,
    settings: ApiSettings,
) -> list[tuple[datetime, datetime]]:
    """Best-effort free-busy fetch (S8.2 decision 3).

    No calendar configured (or configured but disabled) -> ``[]``, native
    slots exactly as S8.1. A provider error (config or network) does NOT fail
    the request -- it is logged as ``calendar_freebusy_degraded`` (warning)
    and native slots are returned; the authoritative double-booking check
    happens at commit (decision 4), not here.
    """
    calendar_config = await get_calendar_config(db, claims)  # type: ignore[arg-type]
    if calendar_config is None or not calendar_config.enabled:
        return []

    try:
        provider = await calendar_provider_for_async(
            calendar_config,
            timeout_seconds=settings.calendar_http_timeout_seconds,
            google_client_id=settings.google_oauth_client_id,
            google_client_secret=settings.google_oauth_client_secret,
        )
        busy = await provider.free_busy(claims, (window_start, window_end))
    except Exception:
        # Covers a Google token-refresh failure (GoogleOAuthError) exactly
        # like any other provider/network error (SR-22) -- this path was
        # already best-effort before the OAuth refresh existed, so no new
        # failure mode changes its posture.
        _log.warning(
            "calendar free-busy degraded to native slots",
            extra={
                "event": "calendar_freebusy_degraded",
                "tenant_id": claims.tenant_id,
                "provider": calendar_config.provider,
            },
        )
        return []

    return [(b.start, b.end) for b in busy]


@router.get("/slots")
async def get_slots(
    request: Request,
    date_from: date | None = Query(default=None),  # noqa: B008
    date_to: date | None = Query(default=None),  # noqa: B008
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> list[SlotResponse]:
    """Return the caller's tenant open slots for ``[date_from, date_to]``.

    Defaults/caps per ``schedule_slot_window_days``/``schedule_slot_window_max_days``.
    No availability configured -> ``[]`` (200, not an error -- S8.1 decision 6).
    """
    db = request.app.state.db
    settings = get_api_settings()

    availability = await get_availability(db, claims)
    if availability is None:
        return []

    start, end = _resolve_window(settings, date_from, date_to)

    zone = ZoneInfo(availability.timezone)
    window_start_utc, _ = _day_bounds_utc(start, zone)
    _, window_end_utc = _day_bounds_utc(end, zone)
    extra_busy = await _calendar_busy_for_window(
        db, claims, window_start_utc, window_end_utc, settings
    )

    slots = await _open_slots_for_window(
        db, claims, availability, start, end,
        window_max_days=settings.schedule_slot_window_max_days,
        extra_busy=extra_busy,
    )

    return [SlotResponse(starts_at=s.starts_at, ends_at=s.ends_at) for s in slots]


@router.get("/availability-summary")
async def get_availability_summary(
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> AvailabilitySummaryResponse:
    """Server-authoritative entry decision and bookable-day map for the widget."""
    db = request.app.state.db
    settings = get_api_settings()

    # SR-6 decision 3: Calendly is detected here, BEFORE any native
    # availability computation or provider construction -- it never
    # participates in calendar_provider_for/free_busy/create_event. A
    # Calendly-configured, enabled tenant with a scheduling_url short-
    # circuits straight to the hosted-handoff action.
    calendar_config = await get_calendar_config(db, claims)
    upcoming = await get_upcoming_booking(db, claims, claims.subject)
    existing = (
        ExistingBookingResponse(
            starts_at=upcoming.starts_at, ends_at=upcoming.ends_at, timezone=upcoming.timezone
        )
        if upcoming is not None else None
    )
    if (
        calendar_config is not None
        and calendar_config.provider == "calendly"
        and calendar_config.enabled
        and calendar_config.scheduling_url
    ):
        return AvailabilitySummaryResponse(
            action="calendly_handoff", timezone="UTC", days=[],
            transition_message=_SCHEDULE_TRANSITION_MESSAGE, existing_booking=existing,
            scheduling_url=calendar_config.scheduling_url,
        )

    availability = await get_availability(db, claims)
    if availability is None:
        return AvailabilitySummaryResponse(
            action="lead_form", timezone="UTC", days=[],
            transition_message=_SCHEDULE_TRANSITION_MESSAGE, existing_booking=existing,
        )

    start, end = _resolve_window(settings, None, None)
    zone = ZoneInfo(availability.timezone)
    window_start, _ = _day_bounds_utc(start, zone)
    _, window_end = _day_bounds_utc(end, zone)
    extra_busy = await _calendar_busy_for_window(db, claims, window_start, window_end, settings)
    slots = await _open_slots_for_window(
        db, claims, availability, start, end,
        window_max_days=settings.schedule_slot_window_max_days, extra_busy=extra_busy,
    )
    available_dates = {slot.starts_at.astimezone(zone).date() for slot in slots}
    days = [
        AvailabilityDayResponse(
            date=start + timedelta(days=offset),
            has_availability=(start + timedelta(days=offset)) in available_dates,
        )
        for offset in range((end - start).days + 1)
    ]
    return AvailabilitySummaryResponse(
        action="schedule_cta", timezone=availability.timezone, days=days,
        transition_message=_SCHEDULE_TRANSITION_MESSAGE, existing_booking=existing,
    )


@router.post("/book", status_code=201)
async def book_slot(
    body: BookRequest,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> BookResponse:
    """Book an open slot for the caller's tenant.

    Consent gate (GDPR): ``consent.granted != True`` -> 422 ``CONSENT_REQUIRED``,
    nothing stored. Re-validates ``starts_at`` is an actually-open slot
    (recomputed for that local day) before inserting -> else 422
    ``SLOT_UNAVAILABLE``. The insert itself is additionally protected by the
    DB partial unique index (``create_event`` catches the race). ``tenant_id``/
    ``visitor_id`` come from the visitor session, never the body.
    """
    if body.consent is None or body.consent.granted is not True:
        raise ValidationError(
            "Consent to store contact information is required.",
            code="CONSENT_REQUIRED",
        )

    db = request.app.state.db
    settings = get_api_settings()

    availability = await get_availability(db, claims)
    if availability is None:
        raise ValidationError(
            "The requested time is no longer available.", code="SLOT_UNAVAILABLE"
        )

    zone = ZoneInfo(availability.timezone)
    starts_at_utc = body.starts_at.astimezone(UTC)
    local_date = starts_at_utc.astimezone(zone).date()

    open_slots = await _open_slots_for_window(
        db, claims, availability, local_date, local_date,
        window_max_days=settings.schedule_slot_window_max_days,
    )
    matching = next((s for s in open_slots if s.starts_at == starts_at_utc), None)
    if matching is None:
        raise ValidationError(
            "The requested time is no longer available.", code="SLOT_UNAVAILABLE"
        )

    consent_with_timestamp = {
        "granted": body.consent.granted,
        "purpose": body.consent.purpose,
        "text": body.consent.text,
        "captured_at": datetime.now(UTC).isoformat(),
    }

    event = await create_event(
        db,
        claims,
        starts_at=matching.starts_at,
        ends_at=matching.ends_at,
        timezone=body.timezone,
        visitor_id=claims.subject,
        lead_id=body.lead_id,
        email=str(body.email) if body.email is not None else None,
        name=body.name,
        consent=consent_with_timestamp,
    )

    _log.info(
        "schedule event booked",
        extra={
            "event": "schedule_event_booked",
            "event_id": event.event_id,
            "tenant_id": claims.tenant_id,
        },
    )

    # Create the 3 reminder rows (3d/24h/1h) immediately after the booking
    # insert and before calendar sync (S8.3 decision 1) -- so an S8.2
    # CALENDAR_SYNC_FAILED compensation (delete_event below) cascades them
    # away via the reminder_jobs FK, leaving no orphaned reminder rows.
    await create_reminder_jobs(
        db, claims, event_id=event.event_id, starts_at=event.starts_at, now=datetime.now(UTC)
    )

    # Populated only when a calendar sync below actually creates a Meet
    # conference (SR-22) -- declared here, outside the try, so it survives
    # into the confirmation-email block further down regardless of whether
    # a calendar is even configured for this tenant.
    meet_url: str | None = None

    calendar_config = await get_calendar_config(db, claims)
    if calendar_config is not None and calendar_config.enabled:
        try:
            provider = await calendar_provider_for_async(
                calendar_config,
                timeout_seconds=settings.calendar_http_timeout_seconds,
                google_client_id=settings.google_oauth_client_id,
                google_client_secret=settings.google_oauth_client_secret,
            )
            ref = await provider.create_event(
                claims,
                CalendarEvent(
                    event_id=event.event_id,
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    timezone=event.timezone,
                    attendee_email=str(body.email) if body.email is not None else None,
                    attendee_name=body.name,
                ),
            )
        except Exception as exc:
            # Covers a Google token-refresh failure (GoogleOAuthError) the
            # same as any other provider/network error (SR-22) -- the
            # existing compensate-and-fail-loud posture already covers it.
            # Never leave a booked row without its calendar event when a
            # calendar is enabled (S8.2 decision 4) -- compensate + fail loud.
            await delete_event(db, claims, event.event_id)
            _log.warning(
                "calendar sync failed, booking compensated",
                extra={
                    "event": "calendar_sync_failed",
                    "event_id": event.event_id,
                    "tenant_id": claims.tenant_id,
                    "provider": calendar_config.provider,
                },
            )
            raise ValidationError(
                "Failed to sync the booking to the calendar. Please try again.",
                code="CALENDAR_SYNC_FAILED",
            ) from exc

        calendar_ref = f"{ref.provider}:{ref.external_id}"
        meet_url = ref.meet_url
        await update_event_calendar_ref(db, claims, event.event_id, calendar_ref, meet_url)

    # Best-effort booking-lead autolink (SR-9.1 C1). Placed AFTER the
    # calendar-sync block so a CALENDAR_SYNC_FAILED compensation (delete_event
    # above + raise) never reaches here -- no lead is created/linked for a
    # rolled-back booking. Runs as a create-or-link (C2, tenant+visitor
    # scoped, newest-first -- idempotent on rebook) followed by a
    # "booked_a_call" activity (C3). A failure here must NEVER fail or roll
    # back the booking (the booking is the customer's real commitment; the
    # lead is a downstream projection, exactly like the confirmation email).
    # Never log email/name/phone (PII) -- only event_id/tenant_id.
    #
    # `lead_id` is pre-initialized to None (SR-9.3 D4/Scope §3) so it is
    # ALWAYS bound below when passed to enqueue_notification, even if this
    # try block raises before ever assigning it -- a degraded autolink must
    # still enqueue the confirmation, with lead_id NULL, never lost.
    lead_id: str | None = None
    try:
        lead_id = await get_lead_id_by_visitor_id(db, claims, claims.subject)
        if lead_id is None:
            lead_id = await create_lead(
                db,
                claims,
                visitor_id=claims.subject,
                name=body.name,
                email=str(body.email) if body.email is not None else None,
                phone=body.phone,
                consent=consent_with_timestamp,
                source="booking",
            )
            # -- Round-robin auto-assignment (SR-20 D1/D2/D3, M3's third
            # call site -- the one a partial fix is most likely to miss).
            # Fail-open, AFTER the durable lead write, and only for the
            # genuinely-new-lead branch of this create-or-link. This whole
            # block is already best-effort (the booking survives regardless,
            # per the surrounding try/except), but assign_lead_fail_open
            # never raises anyway (D3).
            await assign_lead_fail_open(db, claims, lead_id=lead_id)
            # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable lead
            # write. Only for the genuinely-new-lead branch of this
            # create-or-link -- this whole block is already best-effort
            # (booking survives regardless, per the surrounding try/except),
            # but emit_event_safe never raises anyway.
            await emit_event_safe(
                db,
                claims,
                kind="lead_captured",
                category="leads",
                target_type="lead",
                target_id=lead_id,
                payload={"lead_id": lead_id},
                actor_id=None,
            )
            # -- Enqueue email qualification (fire-and-forget): same
            # fail-open convention as assign_lead_fail_open/emit_event_safe
            # above -- covered by this whole block's surrounding try/except,
            # so an enqueue failure degrades to "booking_lead_link_degraded"
            # like everything else here, never fails the booking itself.
            # Only for the genuinely-new-lead branch -- an existing lead
            # (create-or-link found one) was already classified when it was
            # first captured.
            from common.logging import _correlation_id  # noqa: PLC0415, PLC2701

            classify_lead_email.delay(
                tenant_id=claims.tenant_id,
                lead_id=lead_id,
                correlation_id=_correlation_id.get() or "",
            )
        await set_event_lead_id(db, claims, event.event_id, lead_id)
        await add_activity(
            db,
            claims,
            lead_id,
            type="booked_a_call",
            payload={
                "event_id": event.event_id,
                "starts_at": event.starts_at.isoformat(),
                "timezone": event.timezone,
            },
            actor="system",
        )
    except Exception:
        _log.warning(
            "booking_lead_link_degraded",
            extra={
                "event": "booking_lead_link_degraded",
                "event_id": event.event_id,
                "tenant_id": claims.tenant_id,
            },
        )

    # Best-effort booking-confirmation enqueue (S9.2 Decisions 1/3/5). Placed
    # AFTER the calendar-sync block so a CALENDAR_SYNC_FAILED compensation
    # (delete_event above + raise) never reaches here -- no confirmation is
    # enqueued for a rolled-back booking. An enqueue failure must NEVER fail
    # the booking (email is a downstream side effect; reminders backstop it).
    try:
        recipient = await resolve_event_recipient(db, claims, event.event_id)
        if recipient is None:
            _log.warning(
                "booking_confirm_skipped_no_recipient",
                extra={
                    "event": "booking_confirm_skipped_no_recipient",
                    "event_id": event.event_id,
                    "tenant_id": claims.tenant_id,
                },
            )
        else:
            # calendar_config was already fetched above for the calendar-sync
            # step -- reused here regardless of `.enabled`. A Calendly row is
            # deliberately allowed with `enabled=False`: that flag governs
            # ONLY whether availability-summary hands the visitor off to
            # Calendly instead of the native calendar (routes.py, see
            # GET /public/schedule/availability-summary); it is NOT a gate on
            # including the tenant's Calendly link in the confirmation email.
            calendly_link = (
                calendar_config.scheduling_url
                if calendar_config is not None
                and calendar_config.provider == "calendly"
                and calendar_config.scheduling_url
                else None
            )
            confirm_subject, confirm_body = booking_confirmation_message(
                starts_at=event.starts_at,
                timezone=event.timezone,
                calendly_link=calendly_link,
                meet_url=meet_url,
            )
            job_id = await enqueue_notification(
                db,
                claims,
                channel="email",
                recipient=recipient,
                subject=confirm_subject,
                body=confirm_body,
                dedupe_key=f"booking_confirm:{event.event_id}",
                payload={"kind": "booking_confirm", "event_id": event.event_id},
                lead_id=lead_id,
            )
            if job_id is not None:
                from common.logging import _correlation_id  # noqa: PLC0415, PLC2701

                correlation_id = _correlation_id.get() or ""
                send_notification.delay(
                    job_id=job_id,
                    tenant_id=claims.tenant_id,
                    correlation_id=correlation_id,
                )
    except Exception:
        _log.warning(
            "booking_confirm_enqueue_degraded",
            extra={
                "event": "booking_confirm_enqueue_degraded",
                "event_id": event.event_id,
                "tenant_id": claims.tenant_id,
            },
        )

    return BookResponse(
        event_id=event.event_id,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        status=event.status,
    )


@router.post("/handoff-intent", status_code=200)
async def post_handoff_intent(
    body: HandoffIntentRequest,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> HandoffIntentResponse:
    """Record the pre-Calendly-handoff email correlation intent (SR-6 decision 5a).

    Called by the widget right before the Calendly link-out, so the later
    webhook can backfill ``visitor_id`` onto the ingested booking by
    matching this email. ``visitor_id`` is ALWAYS ``claims.subject`` (the
    visitor session), never a body-supplied id. Leak-free 200 (no
    tenant_id/visitor_id echoed).
    """
    db = request.app.state.db
    settings = get_api_settings()

    await create_handoff_intent(
        db,
        claims,
        visitor_id=claims.subject,
        email=body.email,
        ttl_seconds=settings.calendly_handoff_intent_ttl_seconds,
    )

    return HandoffIntentResponse(recorded=True)
