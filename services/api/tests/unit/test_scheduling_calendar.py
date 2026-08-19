"""Unit tests for api.scheduling.calendar (CalendarProvider Protocol + impls).

Covers:
- StubCalendarProvider.free_busy returns the configured intervals verbatim.
- StubCalendarProvider.create_event returns a deterministic CalendarRef.
- StubCalendarProvider.update_event raises NotImplementedError (not wired
  this sprint).
- GoogleCalendarProvider.free_busy parses a mocked freeBusy.query response
  into a Busy list and sends Authorization: Bearer <token>.
- GoogleCalendarProvider.create_event POSTs events.insert and maps the
  response id -> CalendarRef.
- GoogleCalendarProvider non-2xx / network error -> raises.
- calendar_provider_for: unknown provider -> deterministic CalendarConfigError
  (a ValidationError), no network call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from common.errors import ValidationError

from api.scheduling.calendar import (
    Busy,
    CalendarConfigError,
    CalendarEvent,
    CalendarRef,
    GoogleCalendarProvider,
    StubCalendarProvider,
    _extract_meet_url,
    calendar_provider_for,
    calendar_provider_for_async,
)
from api.scheduling.calendar_config_repository import CalendarConfig

_WINDOW = (
    datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
    datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
)


class _StubTransport(httpx.AsyncBaseTransport):
    """httpx transport double that records the request and returns a canned response."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.json_body = json_body or {}
        self.raise_exc = raise_exc
        self.captured_request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.captured_request = request
        if self.raise_exc is not None:
            raise self.raise_exc
        return httpx.Response(
            status_code=self.status_code, json=self.json_body, request=request
        )


async def _post_via_stub(monkeypatch: pytest.MonkeyPatch, transport: _StubTransport) -> None:
    """Patch httpx.AsyncClient construction inside api.scheduling.calendar."""
    import api.scheduling.calendar as calendar_mod

    original_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return original_client(transport=transport, timeout=5.0)

    monkeypatch.setattr(calendar_mod.httpx, "AsyncClient", _client_factory)


# ==============================================================================
# StubCalendarProvider
# ==============================================================================


async def test_stub_free_busy_returns_configured_intervals() -> None:
    provider = StubCalendarProvider(
        calendar_id="dev",
        busy=[
            {"start": "2026-07-15T14:00:00+00:00", "end": "2026-07-15T14:30:00+00:00"},
        ],
    )

    result = await provider.free_busy(None, _WINDOW)  # type: ignore[arg-type]

    assert result == [
        Busy(
            start=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
            end=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        )
    ]


async def test_stub_free_busy_empty_config_returns_empty_list() -> None:
    provider = StubCalendarProvider(calendar_id="dev", busy=[])

    result = await provider.free_busy(None, _WINDOW)  # type: ignore[arg-type]

    assert result == []


async def test_stub_create_event_returns_deterministic_calendar_ref() -> None:
    provider = StubCalendarProvider(calendar_id="dev", busy=[])
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
    )

    ref = await provider.create_event(None, event)  # type: ignore[arg-type]

    assert isinstance(ref, CalendarRef)
    assert ref.provider == "stub"
    assert ref.external_id == "stub-evt-1"
    assert ref.meet_url == "https://meet.google.com/stub-evt-1"


async def test_stub_update_event_raises_not_implemented() -> None:
    provider = StubCalendarProvider(calendar_id="dev", busy=[])
    ref = CalendarRef(provider="stub", external_id="stub-evt-1")
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
    )

    with pytest.raises(NotImplementedError):
        await provider.update_event(None, ref, event)  # type: ignore[arg-type]


# ==============================================================================
# GoogleCalendarProvider
# ==============================================================================


async def test_google_free_busy_parses_response_and_sends_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _StubTransport(
        status_code=200,
        json_body={
            "calendars": {
                "primary": {
                    "busy": [
                        {"start": "2026-07-15T14:00:00Z", "end": "2026-07-15T14:30:00Z"},
                    ]
                }
            }
        },
    )
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(
        calendar_id="primary", access_token="tok-abc123", timeout=5.0
    )

    result = await provider.free_busy(None, _WINDOW)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].start == datetime.fromisoformat("2026-07-15T14:00:00+00:00")

    assert transport.captured_request is not None
    auth_header = transport.captured_request.headers.get("authorization")
    assert auth_header == "Bearer tok-abc123"
    assert "freeBusy" in str(transport.captured_request.url)


async def test_google_free_busy_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(status_code=500)
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(calendar_id="primary", access_token="tok", timeout=5.0)

    with pytest.raises(Exception):  # noqa: B017 -- surfaces as CALENDAR_SYNC_FAILED at the route
        await provider.free_busy(None, _WINDOW)  # type: ignore[arg-type]


async def test_google_free_busy_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(raise_exc=httpx.ConnectError("boom"))
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(calendar_id="primary", access_token="tok", timeout=5.0)

    with pytest.raises(Exception):  # noqa: B017
        await provider.free_busy(None, _WINDOW)  # type: ignore[arg-type]


async def test_google_create_event_posts_and_maps_id(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(status_code=200, json_body={"id": "google-evt-999"})
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(
        calendar_id="primary", access_token="tok-xyz", timeout=5.0
    )
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
    )

    ref = await provider.create_event(None, event)  # type: ignore[arg-type]

    assert ref == CalendarRef(provider="google", external_id="google-evt-999", meet_url=None)
    assert transport.captured_request is not None
    auth_header = transport.captured_request.headers.get("authorization")
    assert auth_header == "Bearer tok-xyz"
    assert "/calendars/primary/events" in str(transport.captured_request.url)


async def test_google_create_event_requests_meet_conference_and_extracts_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SR-22: create_event must ask for a Meet conference (conferenceData
    createRequest + conferenceDataVersion=1), suppress Google's own invite
    email (sendUpdates=none), add the attendee when given, and pull the
    join URL out of the response onto CalendarRef.meet_url."""
    transport = _StubTransport(
        status_code=200,
        json_body={
            "id": "google-evt-999",
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
                ]
            },
        },
    )
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(
        calendar_id="primary", access_token="tok-xyz", timeout=5.0
    )
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
        attendee_email="visitor@example.com",
        attendee_name="Jordan Visitor",
    )

    ref = await provider.create_event(None, event)  # type: ignore[arg-type]

    assert ref.meet_url == "https://meet.google.com/abc-defg-hij"
    assert transport.captured_request is not None

    url = transport.captured_request.url
    assert url.params.get("conferenceDataVersion") == "1"
    assert url.params.get("sendUpdates") == "none"

    import json as _json

    body = _json.loads(transport.captured_request.content.decode())
    assert body["conferenceData"]["createRequest"]["requestId"] == "evt-1"
    assert body["attendees"] == [{"email": "visitor@example.com"}]
    assert body["summary"] == "Call with Jordan Visitor"
    assert body["description"] == "Name: Jordan Visitor"


async def test_google_create_event_includes_name_and_phone_in_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer's name + phone (already collected in the booking form)
    must reach the Calendar event description, not just the summary --
    the sales team needs the phone number visible directly on the event."""
    transport = _StubTransport(status_code=200, json_body={"id": "google-evt-999"})
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(
        calendar_id="primary", access_token="tok-xyz", timeout=5.0
    )
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
        attendee_email="visitor@example.com",
        attendee_name="Jordan Visitor",
        attendee_phone="+15551234567",
    )

    await provider.create_event(None, event)  # type: ignore[arg-type]

    import json as _json

    assert transport.captured_request is not None
    body = _json.loads(transport.captured_request.content.decode())
    assert body["description"] == "Name: Jordan Visitor\nPhone: +15551234567"


async def test_google_create_event_without_attendee_omits_attendees_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _StubTransport(status_code=200, json_body={"id": "google-evt-999"})
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(
        calendar_id="primary", access_token="tok-xyz", timeout=5.0
    )
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
    )

    await provider.create_event(None, event)  # type: ignore[arg-type]

    import json as _json

    assert transport.captured_request is not None
    body = _json.loads(transport.captured_request.content.decode())
    assert "attendees" not in body
    assert "description" not in body
    assert body["summary"] == "Scheduled call"


async def test_google_create_event_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(status_code=503)
    await _post_via_stub(monkeypatch, transport)

    provider = GoogleCalendarProvider(calendar_id="primary", access_token="tok", timeout=5.0)
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
    )

    with pytest.raises(Exception):  # noqa: B017
        await provider.create_event(None, event)  # type: ignore[arg-type]


async def test_google_update_event_raises_not_implemented() -> None:
    provider = GoogleCalendarProvider(calendar_id="primary", access_token="tok", timeout=5.0)
    ref = CalendarRef(provider="google", external_id="google-evt-999")
    event = CalendarEvent(
        event_id="evt-1",
        starts_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 15, 14, 30, tzinfo=UTC),
        timezone="UTC",
    )

    with pytest.raises(NotImplementedError):
        await provider.update_event(None, ref, event)  # type: ignore[arg-type]


# ==============================================================================
# calendar_provider_for
# ==============================================================================


def test_calendar_provider_for_stub_returns_stub_provider() -> None:
    config = CalendarConfig(
        provider="stub", calendar_id="dev", credentials="tok", busy=[], enabled=True
    )
    provider = calendar_provider_for(config, timeout=5.0)
    assert isinstance(provider, StubCalendarProvider)


def test_calendar_provider_for_google_returns_google_provider() -> None:
    config = CalendarConfig(
        provider="google", calendar_id="primary", credentials="tok", busy=[], enabled=True
    )
    provider = calendar_provider_for(config, timeout=5.0)
    assert isinstance(provider, GoogleCalendarProvider)


def test_calendar_provider_for_unknown_provider_raises_deterministic_error() -> None:
    config = CalendarConfig(
        provider="outlook", calendar_id=None, credentials="tok", busy=[], enabled=True
    )
    with pytest.raises(ValidationError):
        calendar_provider_for(config, timeout=5.0)

    with pytest.raises(CalendarConfigError):
        calendar_provider_for(config, timeout=5.0)


# ==============================================================================
# _extract_meet_url
# ==============================================================================


def test_extract_meet_url_returns_video_entry_point_uri() -> None:
    response = {
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
            ]
        }
    }
    assert _extract_meet_url(response) == "https://meet.google.com/abc-defg-hij"


def test_extract_meet_url_missing_conference_data_returns_none() -> None:
    assert _extract_meet_url({"id": "evt-1"}) is None


def test_extract_meet_url_conference_data_not_a_dict_returns_none() -> None:
    assert _extract_meet_url({"conferenceData": "pending"}) is None


def test_extract_meet_url_missing_entry_points_returns_none() -> None:
    assert _extract_meet_url({"conferenceData": {}}) is None


def test_extract_meet_url_entry_points_not_a_list_returns_none() -> None:
    assert _extract_meet_url({"conferenceData": {"entryPoints": "oops"}}) is None


def test_extract_meet_url_no_video_entry_returns_none() -> None:
    response = {
        "conferenceData": {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1-555-0100"}]}
    }
    assert _extract_meet_url(response) is None


def test_extract_meet_url_video_entry_missing_uri_returns_none() -> None:
    response = {"conferenceData": {"entryPoints": [{"entryPointType": "video"}]}}
    assert _extract_meet_url(response) is None


# ==============================================================================
# calendar_provider_for_async
# ==============================================================================


async def test_calendar_provider_for_async_stub_delegates_without_refresh() -> None:
    """Non-google providers must never touch google_oauth at all."""
    config = CalendarConfig(
        provider="stub", calendar_id="dev", credentials="tok", busy=[], enabled=True
    )
    provider = await calendar_provider_for_async(
        config, timeout_seconds=5.0, google_client_id=None, google_client_secret=None
    )
    assert isinstance(provider, StubCalendarProvider)


async def test_calendar_provider_for_async_google_refreshes_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.scheduling.calendar as calendar_mod

    captured: dict[str, object] = {}

    async def _fake_refresh(
        *, client_id: str, client_secret: str, refresh_token: str, timeout_seconds: float
    ) -> str:
        captured["client_id"] = client_id
        captured["client_secret"] = client_secret
        captured["refresh_token"] = refresh_token
        captured["timeout_seconds"] = timeout_seconds
        return "freshly-minted-access-token"

    monkeypatch.setattr(calendar_mod, "refresh_google_access_token", _fake_refresh)

    # Fixture values kept out of a bare `google_client_secret="..."` kwarg
    # so they read clearly as test data, not a real credential.
    stored_refresh_value = "stored-refresh-token-value"
    platform_client_id_value = "platform-client-id"
    platform_secret_value = "platform-client-secret"

    config = CalendarConfig(
        provider="google",
        calendar_id="primary",
        credentials=stored_refresh_value,
        busy=[],
        enabled=True,
    )

    provider = await calendar_provider_for_async(
        config,
        timeout_seconds=7.5,
        google_client_id=platform_client_id_value,
        google_client_secret=platform_secret_value,
    )

    assert isinstance(provider, GoogleCalendarProvider)
    assert captured["refresh_token"] == stored_refresh_value
    assert captured["client_id"] == platform_client_id_value
    assert captured["client_secret"] == platform_secret_value
    assert captured["timeout_seconds"] == 7.5
    # The provider must be constructed with the FRESH access token, never
    # the long-lived refresh token that was stored.
    assert provider._access_token == "freshly-minted-access-token"  # noqa: SLF001


async def test_calendar_provider_for_async_google_missing_client_id_raises() -> None:
    config = CalendarConfig(
        provider="google", calendar_id="primary", credentials="tok", busy=[], enabled=True
    )
    with pytest.raises(CalendarConfigError):
        await calendar_provider_for_async(
            config, timeout_seconds=5.0, google_client_id=None, google_client_secret="secret"
        )


async def test_calendar_provider_for_async_google_missing_client_secret_raises() -> None:
    config = CalendarConfig(
        provider="google", calendar_id="primary", credentials="tok", busy=[], enabled=True
    )
    with pytest.raises(CalendarConfigError):
        await calendar_provider_for_async(
            config, timeout_seconds=5.0, google_client_id="client-id", google_client_secret=None
        )
