"""Unit tests for api.scheduling.google_oauth (SR-22).

Covers:
- build_google_authorize_url: correct base URL + every required query param
  (client_id, redirect_uri, response_type, scope, access_type=offline,
  prompt=consent, state).
- exchange_google_auth_code: success -> GoogleTokens; missing refresh_token
  -> GoogleOAuthError; non-2xx -> GoogleOAuthError; network error ->
  GoogleOAuthError.
- refresh_google_access_token: success -> access_token string; non-2xx ->
  GoogleOAuthError; network error -> GoogleOAuthError.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from api.scheduling.google_oauth import (
    GOOGLE_CALENDAR_SCOPE,
    GoogleOAuthError,
    GoogleTokens,
    build_google_authorize_url,
    exchange_google_auth_code,
    refresh_google_access_token,
)


class _StubTransport(httpx.AsyncBaseTransport):
    """httpx transport double -- mirrors test_scheduling_calendar.py's own,
    so this module's HTTP tests read the same way as calendar.py's."""

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
        return httpx.Response(status_code=self.status_code, json=self.json_body, request=request)


async def _post_via_stub(monkeypatch: pytest.MonkeyPatch, transport: _StubTransport) -> None:
    import api.scheduling.google_oauth as oauth_mod

    original_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return original_client(transport=transport, timeout=5.0)

    monkeypatch.setattr(oauth_mod.httpx, "AsyncClient", _client_factory)


# ==============================================================================
# build_google_authorize_url
# ==============================================================================


def test_build_authorize_url_includes_every_required_param() -> None:
    url = build_google_authorize_url(
        client_id="client-123",
        redirect_uri="https://api.example.com/admin/schedule/calendar/google/callback",
        state="state-abc",
    )

    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"

    params = parse_qs(parsed.query)
    assert params["client_id"] == ["client-123"]
    assert params["redirect_uri"] == [
        "https://api.example.com/admin/schedule/calendar/google/callback"
    ]
    assert params["response_type"] == ["code"]
    assert params["scope"] == [GOOGLE_CALENDAR_SCOPE]
    # Both load-bearing for correctness, not just nice-to-haves (see the
    # function's own docstring): access_type=offline is what makes Google
    # issue a refresh token at all; prompt=consent forces one even on a
    # re-authorization.
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["state"] == ["state-abc"]


# ==============================================================================
# exchange_google_auth_code
# ==============================================================================


async def test_exchange_success_returns_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(
        status_code=200,
        json_body={
            "access_token": "access-xyz",
            "refresh_token": "refresh-xyz",
            "expires_in": 3599,
        },
    )
    await _post_via_stub(monkeypatch, transport)

    tokens = await exchange_google_auth_code(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="https://api.example.com/callback",
        code="auth-code-1",
        timeout_seconds=5.0,
    )

    assert tokens == GoogleTokens(
        access_token="access-xyz", refresh_token="refresh-xyz", expires_in=3599
    )
    assert transport.captured_request is not None
    assert transport.captured_request.method == "POST"
    body = transport.captured_request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=auth-code-1" in body


async def test_exchange_missing_refresh_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google omits refresh_token on a repeat consent without prompt=consent
    -- since this module always sets that, a missing refresh_token here
    means something diverged; must fail loud, never silently proceed."""
    transport = _StubTransport(
        status_code=200, json_body={"access_token": "access-xyz", "expires_in": 3599}
    )
    await _post_via_stub(monkeypatch, transport)

    with pytest.raises(GoogleOAuthError):
        await exchange_google_auth_code(
            client_id="cid",
            client_secret="csecret",
            redirect_uri="https://api.example.com/callback",
            code="auth-code-1",
            timeout_seconds=5.0,
        )


async def test_exchange_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(status_code=400, json_body={"error": "invalid_grant"})
    await _post_via_stub(monkeypatch, transport)

    with pytest.raises(GoogleOAuthError):
        await exchange_google_auth_code(
            client_id="cid",
            client_secret="csecret",
            redirect_uri="https://api.example.com/callback",
            code="bad-code",
            timeout_seconds=5.0,
        )


async def test_exchange_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(raise_exc=httpx.ConnectError("boom"))
    await _post_via_stub(monkeypatch, transport)

    with pytest.raises(GoogleOAuthError):
        await exchange_google_auth_code(
            client_id="cid",
            client_secret="csecret",
            redirect_uri="https://api.example.com/callback",
            code="auth-code-1",
            timeout_seconds=5.0,
        )


# ==============================================================================
# refresh_google_access_token
# ==============================================================================


async def test_refresh_success_returns_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(
        status_code=200, json_body={"access_token": "fresh-access-token", "expires_in": 3599}
    )
    await _post_via_stub(monkeypatch, transport)

    # Fixture value for a stored refresh token, kept out of a bare
    # `refresh_token="..."` kwarg so it reads clearly as test data, not a
    # real credential.
    stored_refresh_value = "stored-refresh-token"
    access_token = await refresh_google_access_token(
        client_id="cid",
        client_secret="csecret",
        refresh_token=stored_refresh_value,
        timeout_seconds=5.0,
    )

    assert access_token == "fresh-access-token"
    assert transport.captured_request is not None
    body = transport.captured_request.content.decode()
    assert "grant_type=refresh_token" in body
    assert f"refresh_token={stored_refresh_value}" in body


async def test_refresh_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked/expired refresh token -> Google returns 400 -> GoogleOAuthError."""
    transport = _StubTransport(status_code=400, json_body={"error": "invalid_grant"})
    await _post_via_stub(monkeypatch, transport)

    revoked_value = "revoked-token"
    with pytest.raises(GoogleOAuthError):
        await refresh_google_access_token(
            client_id="cid",
            client_secret="csecret",
            refresh_token=revoked_value,
            timeout_seconds=5.0,
        )


async def test_refresh_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _StubTransport(raise_exc=httpx.ConnectError("boom"))
    await _post_via_stub(monkeypatch, transport)

    with pytest.raises(GoogleOAuthError):
        await refresh_google_access_token(
            client_id="cid",
            client_secret="csecret",
            refresh_token="tok",
            timeout_seconds=5.0,
        )
