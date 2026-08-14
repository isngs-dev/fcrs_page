"""Google OAuth 2.0 authorization-code + refresh-token exchange (SR-22).

Raw ``httpx`` against Google's OAuth endpoints -- no ``google-auth``/
``google-api-python-client`` dependency, mirroring ``api.scheduling.calendar``'s
own no-client-library posture so the two modules share one HTTP style.

This is a PLATFORM-level OAuth app (one Google Cloud project, one client
ID/secret -- ``ApiSettings.google_oauth_client_id``/``_client_secret``,
env-configured, never per-tenant) that each tenant's ``CLIENT_ADMIN``
authorizes access through individually. The result of that authorization --
a long-lived refresh token -- IS per-tenant, and is what
``tenant_calendar_configs.credentials_ciphertext`` stores (encrypted) for
``provider="google"`` going forward (``api.scheduling.calendar_config_repository``).
Access tokens are never stored anywhere; they're minted fresh from the
refresh token immediately before each Calendar API call
(``api.scheduling.calendar.calendar_provider_for_async``).

Two routes drive the admin-facing half of this flow
(``api.scheduling.admin_routes``):
  - ``GET /admin/schedule/calendar/google/authorize`` -- builds the consent
    URL below and issues a one-time state token
    (``api.scheduling.google_oauth_state``).
  - ``GET /admin/schedule/calendar/google/callback`` -- Google redirects the
    admin's browser back here with ``code``+``state``; the route consumes
    the state (proving it's the same authorize call, once), exchanges the
    code via ``exchange_google_auth_code`` below, and stores the resulting
    refresh token via ``upsert_calendar_config``.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from common.errors import AppException

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 -- a public endpoint URL, not a secret
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


class GoogleOAuthError(AppException):
    """A Google token-endpoint call failed (bad code, revoked refresh token,
    network error) -- 502, mirroring ``LLMError``'s posture for a failed
    upstream call. Never retried automatically: the caller (booking/slots
    routes) already has its own explicit degrade-or-fail behavior per call
    site; blind retry here would just re-hit a very likely-still-bad token.
    """

    code = "GOOGLE_OAUTH_ERROR"
    http_status = 502
    default_message = "Google OAuth request failed."


@dataclass(frozen=True)
class GoogleTokens:
    """The result of exchanging an authorization code for tokens.

    ``refresh_token`` is present ONLY on the first consent for a given
    tenant+scope combination unless the authorize request includes
    ``prompt=consent`` (which ``build_google_authorize_url`` always sets, for
    exactly this reason -- a re-authorization must still yield a usable
    refresh token, not silently omit it).
    """

    access_token: str
    refresh_token: str
    expires_in: int


def build_google_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Google consent-screen URL for the Calendar scope.

    ``access_type=offline`` is what makes Google issue a refresh token at
    all (without it, only a short-lived access token comes back -- useless
    for a backend that needs to act on the tenant's calendar indefinitely).
    ``prompt=consent`` forces the consent screen (and a fresh refresh token)
    even if this tenant already authorized once before -- re-connecting a
    calendar must always work, never silently no-op.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_google_auth_code(
    *, client_id: str, client_secret: str, redirect_uri: str, code: str, timeout_seconds: float,
) -> GoogleTokens:
    """Exchange a one-time authorization ``code`` for tokens.

    Raises ``GoogleOAuthError`` on any non-2xx response, a network error, or
    a response missing ``refresh_token`` (the one case worth naming
    explicitly: Google omits it on a repeat consent WITHOUT
    ``prompt=consent`` -- since ``build_google_authorize_url`` always sets
    that, seeing it missing here means something about the request diverged
    from what this module built, worth failing loud on rather than silently
    storing an unusable config).
    """
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(f"Google token exchange request failed: {exc}") from exc

    if not (200 <= response.status_code < 300):
        raise GoogleOAuthError(
            f"Google token exchange returned non-2xx status: {response.status_code}"
        )

    data = response.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise GoogleOAuthError(
            "Google token exchange did not return a refresh_token -- "
            "re-authorize with the consent screen shown again."
        )

    return GoogleTokens(
        access_token=str(data["access_token"]),
        refresh_token=str(refresh_token),
        expires_in=int(data.get("expires_in", 3600)),
    )


async def refresh_google_access_token(
    *, client_id: str, client_secret: str, refresh_token: str, timeout_seconds: float,
) -> str:
    """Exchange a stored refresh token for a fresh access token.

    Called immediately before every real Calendar API request
    (``api.scheduling.calendar.calendar_provider_for_async``) -- access
    tokens are never cached or stored, only ever minted just-in-time.
    Raises ``GoogleOAuthError`` on any non-2xx response or network error
    (e.g. a revoked/expired refresh token) -- the caller's existing
    CALENDAR_SYNC_FAILED-with-compensation (booking) or
    calendar_freebusy_degraded (slots) handling applies unchanged, since
    both already catch broadly around calendar-provider construction.
    """
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(f"Google token refresh request failed: {exc}") from exc

    if not (200 <= response.status_code < 300):
        raise GoogleOAuthError(
            f"Google token refresh returned non-2xx status: {response.status_code}"
        )

    data = response.json()
    return str(data["access_token"])
