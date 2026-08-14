"""Admin scheduling routes -- PUT /admin/schedule/availability (CLIENT_ADMIN only).

Sets a tenant's availability rules + IANA timezone (S8.1 decision 1).
``CLIENT_AGENT``/``VISITOR`` are forbidden (403) -- availability is
configuration, not review, and per CLAUDE.md RBAC CLIENT_AGENT cannot change
config. An invalid IANA timezone or malformed rules shape is rejected with
422 before anything is persisted.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from common.auth import AuthClaims, Role
from common.errors import ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

from api.auth.dependencies import require_roles
from api.config import get_api_settings
from api.scheduling.calendar_config_repository import upsert_calendar_config
from api.scheduling.google_oauth import (
    GoogleOAuthError,
    build_google_authorize_url,
    exchange_google_auth_code,
)
from api.scheduling.google_oauth_state import get_google_oauth_state_store
from api.scheduling.repository import upsert_availability

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/schedule", tags=["scheduling"])

_WEEKDAY_KEYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class RulesPayload(BaseModel):
    """The ``rules`` jsonb shape (S8.1 decision 1)."""

    slot_minutes: int = Field(gt=0)
    buffer_minutes: int = Field(ge=0)
    weekly_hours: dict[str, list[list[str]]]

    @field_validator("weekly_hours")
    @classmethod
    def _validate_weekly_hours(
        cls, v: dict[str, list[list[str]]]
    ) -> dict[str, list[list[str]]]:
        unknown = set(v.keys()) - _WEEKDAY_KEYS
        if unknown:
            raise ValueError(f"weekly_hours has unknown weekday keys: {sorted(unknown)}")
        for windows in v.values():
            for window in windows:
                if len(window) != 2:
                    raise ValueError("each weekly_hours window must be [start, end]")
                start, end = window
                if not _HHMM_RE.match(start) or not _HHMM_RE.match(end):
                    raise ValueError("weekly_hours times must be 24h HH:MM")
                if start >= end:
                    raise ValueError("weekly_hours window start must be before end")
        return v


class AvailabilityUpsertRequest(BaseModel):
    """Body for PUT /admin/schedule/availability."""

    timezone: str
    rules: RulesPayload

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"invalid IANA timezone: {v}") from exc
        return v


class AvailabilityResponse(BaseModel):
    """Leak-free (no tenant_id) availability for the admin surface."""

    timezone: str
    rules: dict[str, Any]
    updated_at: datetime


@router.put("/availability")
async def put_availability(
    body: AvailabilityUpsertRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> AvailabilityResponse:
    """Set the caller's tenant availability. ``CLIENT_ADMIN`` only.

    Invalid timezone/rules are rejected by ``AvailabilityUpsertRequest``
    validation before this handler runs (422, nothing persisted).
    """
    db = request.app.state.db

    availability = await upsert_availability(
        db, claims, timezone=body.timezone, rules=body.rules.model_dump()
    )

    return AvailabilityResponse(
        timezone=availability.timezone,
        rules=availability.rules,
        updated_at=availability.updated_at,
    )


class BusyIntervalPayload(BaseModel):
    """A single ``StubCalendarProvider``-consumed busy interval (dev/test only)."""

    start: str
    end: str


class CalendarConfigRequest(BaseModel):
    """Body for PUT /admin/schedule/calendar."""

    provider: str
    calendar_id: str | None = None
    credentials: str
    enabled: bool = False
    busy: list[BusyIntervalPayload] = Field(default_factory=list)
    # SR-6: the tenant's Calendly hosted-scheduling page (link-out target).
    # Only meaningful for provider="calendly"; not a secret.
    scheduling_url: str | None = None


class CalendarConfigResponse(BaseModel):
    """Leak-free (no credentials) response for PUT /admin/schedule/calendar."""

    provider: str
    calendar_id: str | None
    enabled: bool
    # scheduling_url is NOT a secret (SR-6) -- safe to echo, unlike credentials.
    scheduling_url: str | None = None


@router.put("/calendar")
async def put_calendar_config(
    body: CalendarConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> CalendarConfigResponse:
    """Set the calling tenant's calendar provider + credentials. ``CLIENT_ADMIN`` only.

    The OAuth access token / ``StubCalendarProvider`` secret is encrypted at
    rest (AES-256-GCM via ``SecretBox``) and never echoed back in the response
    (S8.2 decision 2).
    """
    await upsert_calendar_config(
        request.app.state.db,
        claims,
        provider=body.provider,
        calendar_id=body.calendar_id,
        credentials=body.credentials,
        busy=[interval.model_dump() for interval in body.busy],
        enabled=body.enabled,
        scheduling_url=body.scheduling_url,
    )

    _log.info(
        "calendar config updated",
        extra={
            "event": "calendar_config_set",
            "provider": body.provider,
            "tenant_id": claims.tenant_id,
            "enabled": body.enabled,
        },
    )

    return CalendarConfigResponse(
        provider=body.provider,
        calendar_id=body.calendar_id,
        enabled=body.enabled,
        scheduling_url=body.scheduling_url,
    )


class GoogleAuthorizeResponse(BaseModel):
    """Body for GET /admin/schedule/calendar/google/authorize.

    A JSON body, not an HTTP redirect -- this route is called by admin-web
    (fetch/server action), which then navigates the ADMIN'S OWN browser to
    ``authorize_url`` itself (a plain fetch response's redirects aren't
    exposed the same way to the caller's top-level window).
    """

    authorize_url: str


@router.get("/calendar/google/authorize")
async def google_calendar_authorize(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> GoogleAuthorizeResponse:
    """Start the "Connect Google Calendar" OAuth flow (SR-22). ``CLIENT_ADMIN`` only.

    Issues a one-time ``state`` token bound to the caller's tenant
    (``google_oauth_state.py`` -- Redis, single-use, TTL'd) and returns the
    Google consent-screen URL to send the admin's browser to. Raises a
    deterministic ``GOOGLE_OAUTH_NOT_CONFIGURED`` error (422) up front if the
    platform's own OAuth client isn't configured -- never lets an admin go
    through Google's consent screen only to fail on the callback.
    """
    settings = get_api_settings()
    if not settings.google_oauth_client_id or not settings.google_oauth_redirect_uri:
        raise ValidationError(
            "Google Calendar OAuth is not configured on this deployment.",
            code="GOOGLE_OAUTH_NOT_CONFIGURED",
        )

    assert claims.tenant_id is not None  # noqa: S101 -- require_roles(CLIENT_ADMIN) guarantees this

    state_store = get_google_oauth_state_store(request)
    state = await state_store.issue(claims.tenant_id, settings.google_oauth_state_ttl_seconds)

    authorize_url = build_google_authorize_url(
        client_id=settings.google_oauth_client_id,
        redirect_uri=settings.google_oauth_redirect_uri,
        state=state,
    )
    return GoogleAuthorizeResponse(authorize_url=authorize_url)


@router.get("/calendar/google/callback")
async def google_calendar_callback(
    request: Request,
    code: str | None = Query(default=None),  # noqa: B008
    state: str | None = Query(default=None),  # noqa: B008
    error: str | None = Query(default=None),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> RedirectResponse:
    """Google's OAuth redirect target (SR-22). ``CLIENT_ADMIN`` only.

    Hit by a raw top-level browser navigation FROM Google, not an API
    caller -- the admin's existing session cookie rides along automatically
    (SameSite=Lax explicitly allows a top-level GET navigation to carry it),
    so ``require_roles`` works unchanged here. Every outcome redirects the
    browser back into admin-web's Workspace screen (``workspace
    ?calendar_connected=true`` on success, ``workspace?calendar_error=<reason>``
    otherwise) -- not ``/settings`` (bot persona/behavior) -- since that's
    where the sibling scheduling-availability config
    (``workspace/availability-section.tsx``) already lives, rather than
    returning raw JSON, since a human is looking at this response, not code.

    ``state`` is consumed exactly once (Redis GETDEL) -- a replayed
    callback URL fails on its second use. The consumed state's tenant_id is
    cross-checked against the CALLER's own authenticated tenant_id (defense
    in depth on top of the state token itself): a state issued for tenant A
    can never be completed while authenticated as tenant B.
    """
    settings = get_api_settings()
    workspace_url = f"{settings.admin_web_base_url}/workspace"

    if error:
        _log.warning(
            "google_calendar_oauth_denied",
            extra={"event": "google_calendar_oauth_denied", "tenant_id": claims.tenant_id},
        )
        return RedirectResponse(f"{workspace_url}?calendar_error=access_denied")

    if not code or not state:
        return RedirectResponse(f"{workspace_url}?calendar_error=missing_code_or_state")

    state_store = get_google_oauth_state_store(request)
    state_tenant_id = await state_store.consume(state)
    if state_tenant_id is None or state_tenant_id != claims.tenant_id:
        _log.warning(
            "google_calendar_oauth_state_invalid",
            extra={"event": "google_calendar_oauth_state_invalid", "tenant_id": claims.tenant_id},
        )
        return RedirectResponse(f"{workspace_url}?calendar_error=invalid_state")

    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret \
            or not settings.google_oauth_redirect_uri:
        return RedirectResponse(f"{workspace_url}?calendar_error=not_configured")

    try:
        tokens = await exchange_google_auth_code(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri=settings.google_oauth_redirect_uri,
            code=code,
            timeout_seconds=settings.calendar_http_timeout_seconds,
        )
    except GoogleOAuthError:
        _log.warning(
            "google_calendar_oauth_exchange_failed",
            extra={"event": "google_calendar_oauth_exchange_failed", "tenant_id": claims.tenant_id},
        )
        return RedirectResponse(f"{workspace_url}?calendar_error=exchange_failed")

    # The refresh token is the ONLY thing stored -- never the access token
    # this exchange also returned (api.scheduling.calendar.calendar_provider_for_async
    # mints a fresh access token from this refresh token immediately before
    # every real Calendar API call, see its own docstring for why).
    await upsert_calendar_config(
        request.app.state.db,
        claims,
        provider="google",
        calendar_id="primary",
        credentials=tokens.refresh_token,
        busy=[],
        enabled=True,
    )

    _log.info(
        "google_calendar_connected",
        extra={"event": "google_calendar_connected", "tenant_id": claims.tenant_id},
    )
    return RedirectResponse(f"{workspace_url}?calendar_connected=true")
