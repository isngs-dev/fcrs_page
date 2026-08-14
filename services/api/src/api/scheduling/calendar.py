"""CalendarProvider Protocol + Busy/CalendarRef/CalendarEvent + impls (S8.2;
Google Meet + OAuth refresh added SR-22).

``CalendarProvider`` is a ``typing.Protocol`` with two implementations this
sprint: ``StubCalendarProvider`` (config-driven, deterministic -- dev/live
testable without a real Google account) and ``GoogleCalendarProvider`` (raw
httpx against Google Calendar API v3; no google client library dependency).
``update_event`` is declared on the Protocol for the future (reschedule/
cancel sync) but is NOT wired by any route this sprint -- both impls raise
``NotImplementedError``.

``calendar_provider_for`` selects an implementation from a tenant's decrypted
``CalendarConfig`` (``api.scheduling.calendar_config_repository``). An
unknown ``provider`` value is a deterministic configuration error --
``CalendarConfigError`` (a ``ValidationError``) -- raised before any network
call, mirroring ``api.crm.sync.crm_sync_for``.

SR-22 (Google Meet + OAuth refresh): ``GoogleCalendarProvider.create_event``
now requests ``conferenceData`` (a Google Meet conference) and returns its
join URL on ``CalendarRef.meet_url``. Its constructor still takes a plain
``access_token`` -- unchanged, so its existing tests stay valid -- because
access tokens expire in ~1 hour and this codebase has nowhere else to refresh
one from. That refresh now happens ONE layer up: for ``provider="google"``,
``CalendarConfig.credentials`` holds a long-lived OAuth **refresh token**
(obtained via the admin ``/admin/schedule/calendar/google/authorize`` +
``/callback`` flow, ``api.scheduling.google_oauth``), and the new async
``calendar_provider_for_async`` exchanges it for a fresh access token
(``api.scheduling.google_oauth.refresh_google_access_token``) immediately
before constructing the provider. Every other provider (``stub``,
``calendly``) is untouched by this -- ``calendar_provider_for`` (sync) is
still the single source of truth for provider selection either way.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

import httpx
from common.auth import AuthClaims
from common.errors import ValidationError

from api.scheduling.calendar_config_repository import CalendarConfig
from api.scheduling.google_oauth import refresh_google_access_token

_GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


@dataclass(frozen=True)
class Busy:
    """A single busy interval on a calendar, UTC."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class CalendarRef:
    """The result of a successful ``CalendarProvider.create_event`` call.

    ``meet_url`` (SR-22) is the Google Meet join link when the provider
    created one -- ``None`` for providers/configs that don't (``stub`` unless
    given a fake one, any provider where conference creation wasn't
    requested or Google omitted ``conferenceData`` from the response).
    Never fabricated: only ever set from a real provider response.
    """

    provider: str
    external_id: str
    meet_url: str | None = None


@dataclass(frozen=True)
class CalendarEvent:
    """The booking payload passed to ``CalendarProvider.create_event``.

    ``attendee_email``/``attendee_name`` (SR-22) are optional so existing
    callers/tests that only exercised the bare event are unaffected; when
    given, ``GoogleCalendarProvider`` adds the visitor as a real Calendar
    attendee (visible to the sales team on the event) and personalizes the
    summary, but sends the request with ``sendUpdates=none`` -- Google's own
    invite email is deliberately suppressed. The notification-service
    confirmation (consent-gated, unsubscribe-aware, delivery-tracked in
    ``notification_jobs``) is the ONE visitor-facing email channel; letting
    Google independently email the attendee would bypass all of that.
    """

    event_id: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    attendee_email: str | None = None
    attendee_name: str | None = None


class CalendarProvider(Protocol):
    """Outbound calendar sync contract. Implementations are selected per tenant."""

    async def free_busy(
        self, claims: AuthClaims, window: tuple[datetime, datetime]
    ) -> list[Busy]: ...

    async def create_event(self, claims: AuthClaims, event: CalendarEvent) -> CalendarRef: ...

    async def update_event(
        self, claims: AuthClaims, ref: CalendarRef, event: CalendarEvent
    ) -> None: ...


class CalendarConfigError(ValidationError):
    """Deterministic calendar config error -- raised before any network call."""

    code = "CALENDAR_CONFIG_ERROR"


class StubCalendarProvider:
    """``CalendarProvider`` implementation: config-driven, deterministic.

    ``free_busy`` returns the tenant's configured ``busy`` intervals verbatim
    (S8.2 decision 1) -- no network I/O, fully dev/live-testable without a
    real calendar account. ``create_event`` returns a fake, deterministic
    ``CalendarRef`` derived from the booking's ``event_id``.
    """

    def __init__(self, *, calendar_id: str, busy: list[dict[str, object]]) -> None:
        self._calendar_id = calendar_id
        self._busy = busy

    async def free_busy(
        self, claims: AuthClaims, window: tuple[datetime, datetime]
    ) -> list[Busy]:
        return [
            Busy(
                start=datetime.fromisoformat(str(interval["start"])),
                end=datetime.fromisoformat(str(interval["end"])),
            )
            for interval in self._busy
        ]

    async def create_event(self, claims: AuthClaims, event: CalendarEvent) -> CalendarRef:
        # A deterministic fake Meet URL (SR-22) -- lets the full booking ->
        # confirmation-email flow be exercised end-to-end in dev/tests
        # without any real Google OAuth setup.
        return CalendarRef(
            provider="stub",
            external_id=f"stub-{event.event_id}",
            meet_url=f"https://meet.google.com/stub-{event.event_id}",
        )

    async def update_event(
        self, claims: AuthClaims, ref: CalendarRef, event: CalendarEvent
    ) -> None:
        raise NotImplementedError("StubCalendarProvider.update_event is not wired this sprint")


def _extract_meet_url(event_response: dict[str, object]) -> str | None:
    """Pull the Meet join URL out of a Calendar ``events.insert`` response.

    ``conferenceData.entryPoints`` is a list (Google Calendar can attach
    phone/SIP entry points alongside the video one) -- find the
    ``entryPointType == "video"`` entry and return its ``uri``. Returns
    ``None`` for any shape that doesn't match (conference creation still
    pending, ``conferenceDataVersion`` wasn't honored, malformed response)
    rather than raising -- a missing Meet link must never fail the booking
    that already succeeded.
    """
    conference_data = event_response.get("conferenceData")
    if not isinstance(conference_data, dict):
        return None
    entry_points = conference_data.get("entryPoints")
    if not isinstance(entry_points, list):
        return None
    for entry in entry_points:
        if isinstance(entry, dict) and entry.get("entryPointType") == "video":
            uri = entry.get("uri")
            return str(uri) if uri else None
    return None


class GoogleCalendarProvider:
    """``CalendarProvider`` implementation: Google Calendar API v3 via raw httpx.

    Uses a decrypted, already-acquired OAuth access token
    (``Authorization: Bearer <token>``) -- this class itself never refreshes
    one; ``calendar_provider_for_async`` (below) is what turns the tenant's
    stored refresh token into a fresh access token immediately before
    constructing this. Non-2xx responses and network errors raise (the
    caller -- the booking route -- surfaces this as ``CALENDAR_SYNC_FAILED``
    with compensation; the slots route degrades to native slots).
    """

    def __init__(self, *, calendar_id: str, access_token: str, timeout: float) -> None:
        self._calendar_id = calendar_id
        self._access_token = access_token
        self._timeout = timeout

    async def free_busy(
        self, claims: AuthClaims, window: tuple[datetime, datetime]
    ) -> list[Busy]:
        window_start, window_end = window
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    f"{_GOOGLE_CALENDAR_API_BASE}/freeBusy",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    json={
                        "timeMin": window_start.isoformat(),
                        "timeMax": window_end.isoformat(),
                        "items": [{"id": self._calendar_id}],
                    },
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Google freeBusy.query request failed: {exc}") from exc

        if not (200 <= response.status_code < 300):
            raise RuntimeError(
                f"Google freeBusy.query returned non-2xx status: {response.status_code}"
            )

        data = response.json()
        busy_intervals = data.get("calendars", {}).get(self._calendar_id, {}).get("busy", [])
        return [
            Busy(
                start=datetime.fromisoformat(str(interval["start"])),
                end=datetime.fromisoformat(str(interval["end"])),
            )
            for interval in busy_intervals
        ]

    async def create_event(self, claims: AuthClaims, event: CalendarEvent) -> CalendarRef:
        summary = f"Call with {event.attendee_name}" if event.attendee_name else "Scheduled call"
        body: dict[str, object] = {
            "start": {"dateTime": event.starts_at.isoformat()},
            "end": {"dateTime": event.ends_at.isoformat()},
            "summary": summary,
            # A Meet conference is requested on every event this creates --
            # requestId must be unique per creation but stable across a
            # retried request for the SAME booking, so Google treats a retry
            # as idempotent instead of minting a second conference; event_id
            # (already unique per booking) is exactly that.
            "conferenceData": {"createRequest": {"requestId": event.event_id}},
        }
        if event.attendee_email:
            body["attendees"] = [{"email": event.attendee_email}]

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    f"{_GOOGLE_CALENDAR_API_BASE}/calendars/{self._calendar_id}/events",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    params={"conferenceDataVersion": 1, "sendUpdates": "none"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Google events.insert request failed: {exc}") from exc

        if not (200 <= response.status_code < 300):
            raise RuntimeError(
                f"Google events.insert returned non-2xx status: {response.status_code}"
            )

        data = response.json()
        return CalendarRef(
            provider="google",
            external_id=str(data["id"]),
            meet_url=_extract_meet_url(data),
        )

    async def update_event(
        self, claims: AuthClaims, ref: CalendarRef, event: CalendarEvent
    ) -> None:
        raise NotImplementedError("GoogleCalendarProvider.update_event is not wired this sprint")


def calendar_provider_for(config: CalendarConfig, *, timeout: float) -> CalendarProvider:
    """Select a ``CalendarProvider`` implementation for the tenant's config.

    Raises ``CalendarConfigError`` (a ``ValidationError``) for an unknown
    ``provider`` value -- deterministic, never retried, never a network call.
    """
    if config.provider == "stub":
        return StubCalendarProvider(
            calendar_id=config.calendar_id or "stub", busy=config.busy
        )

    if config.provider == "google":
        return GoogleCalendarProvider(
            calendar_id=config.calendar_id or "primary",
            access_token=config.credentials,
            timeout=timeout,
        )

    raise CalendarConfigError(
        f"Unsupported calendar provider: {config.provider!r}.",
        code="CALENDAR_PROVIDER_NOT_SUPPORTED",
    )


async def calendar_provider_for_async(
    config: CalendarConfig,
    *,
    timeout_seconds: float,
    google_client_id: str | None,
    google_client_secret: str | None,
) -> CalendarProvider:
    """The real call sites' entry point (SR-22) -- like ``calendar_provider_for``,
    but for ``provider="google"`` first exchanges the stored refresh token
    (``config.credentials``) for a fresh access token via
    ``api.scheduling.google_oauth.refresh_google_access_token``, since access
    tokens expire in ~1 hour and nothing else in this codebase refreshes one.
    Every other provider is untouched -- this delegates straight to the sync
    ``calendar_provider_for`` either way, which remains the single place
    provider SELECTION happens.

    (Named ``timeout_seconds``, not ``timeout`` -- ruff's ASYNC109 flags a
    bare ``timeout`` parameter on an async function as a footgun even though
    this one is legitimately just forwarded to ``httpx``'s own per-request
    timeout, the same as every other ``timeout`` in this module; renaming is
    the accepted way to tell the linter that's intentional here, matching
    ``google_oauth.py``'s two token functions.)

    Raises ``CalendarConfigError`` if Google OAuth isn't configured
    platform-wide (``GOOGLE_OAUTH_CLIENT_ID``/``_SECRET`` unset) -- same
    deterministic-config-error posture as an unknown provider value, and
    raised before any network call.
    """
    if config.provider == "google":
        if not google_client_id or not google_client_secret:
            raise CalendarConfigError(
                "Google Calendar requires GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET "
                "to be configured.",
                code="GOOGLE_OAUTH_NOT_CONFIGURED",
            )
        access_token = await refresh_google_access_token(
            client_id=google_client_id,
            client_secret=google_client_secret,
            refresh_token=config.credentials,
            timeout_seconds=timeout_seconds,
        )
        config = replace(config, credentials=access_token)

    return calendar_provider_for(config, timeout=timeout_seconds)
