"""Unit tests for PUT /admin/schedule/availability and the Google Calendar
OAuth connect routes (SR-22).

Covers:
- PUT /admin/schedule/availability: CLIENT_ADMIN -> 200, availability
  upserted; CLIENT_AGENT / VISITOR -> 403; no auth -> 401; invalid IANA
  timezone -> 422, nothing persisted; invalid rules shape (bad HH:MM,
  start>=end, slot_minutes<=0, buffer_minutes<0, unknown weekday key) ->
  422; tenant-scoped, two tenants' availability never collide.
- GET /admin/schedule/calendar/google/authorize: CLIENT_ADMIN -> 200 +
  authorize_url + one state issued for the caller's tenant; CLIENT_AGENT /
  VISITOR -> 403; no auth -> 401; OAuth not configured -> 422
  GOOGLE_OAUTH_NOT_CONFIGURED.
- GET /admin/schedule/calendar/google/callback: success -> redirect with
  calendar_connected=true + calendar config stored (encrypted) for the
  caller's tenant; every error branch (access_denied, missing_code_or_state,
  invalid_state, not_configured, exchange_failed) -> redirect with the
  matching calendar_error; CLIENT_AGENT / VISITOR -> 403; a state issued for
  tenant A cannot be consumed while authenticated as tenant B (tenant
  isolation).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"
_OTHER_TENANT_ID = "tenant-xyz-999"

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

_VALID_BODY = {
    "timezone": "America/New_York",
    "rules": {
        "slot_minutes": 30,
        "buffer_minutes": 0,
        "weekly_hours": {
            "mon": [["09:00", "17:00"]], "tue": [["09:00", "17:00"]],
            "wed": [["09:00", "17:00"]], "thu": [["09:00", "17:00"]],
            "fri": [["09:00", "17:00"]], "sat": [], "sun": [],
        },
    },
}


class _StubDatabase:
    def __init__(self) -> None:
        self._availability: dict[str, dict[str, Any]] = {}
        self.calendar_configs: dict[str, dict[str, Any]] = {}

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO AVAILABILITY"):
            tenant_id, timezone, rules = args
            self._availability[tenant_id] = {
                "tenant_id": tenant_id, "timezone": timezone, "rules": rules,
            }
            return "INSERT 0 1"
        if q.startswith("INSERT INTO TENANT_CALENDAR_CONFIGS"):
            tenant_id, provider, calendar_id, ciphertext, busy, enabled, scheduling_url = args
            self.calendar_configs[tenant_id] = {
                "tenant_id": tenant_id,
                "provider": provider,
                "calendar_id": calendar_id,
                "credentials_ciphertext": ciphertext,
                "busy": busy,
                "enabled": enabled,
                "scheduling_url": scheduling_url,
            }
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM AVAILABILITY" in q:
            tenant_id = args[0]
            row = self._availability.get(tenant_id)
            if row is None:
                return None
            from datetime import UTC, datetime

            return {**row, "updated_at": datetime(2026, 1, 1, tzinfo=UTC)}
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
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


class _RecordingRedis:
    """Stateful Redis double: real set/getdel, so the OAuth state store's
    issue -> consume roundtrip (google_oauth_state.py) actually works across
    the two separate requests these tests make."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.set_calls.append((key, value, ex))
        self._store[key] = value

    async def getdel(self, key: str) -> str | None:
        return self._store.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app(
    db: _StubDatabase, *, redis: Any = None, extra_env: dict[str, str] | None = None
) -> Any:
    _reset_settings()

    env = {**_TEST_SETTINGS_ENV, **(extra_env or {})}
    with patch.dict("os.environ", env, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db
    app.state.redis = redis if redis is not None else _StubRedis()
    app.state.cache = InMemoryCache()
    return app


_GOOGLE_OAUTH_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "platform-client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "platform-client-secret-value",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://api.example.com/admin/schedule/calendar/google/callback",
    "ADMIN_WEB_BASE_URL": "https://admin.example.com",
}


def _token(role: Role, tenant_id: str | None = _TENANT_ID) -> str:
    claims = AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


async def test_client_admin_can_set_availability() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/schedule/availability", json=_VALID_BODY, cookies={"access_token": token}
        )

    assert response.status_code == 200
    data = response.json()
    assert data["timezone"] == "America/New_York"
    assert "tenant_id" not in data
    assert db._availability[_TENANT_ID]["timezone"] == "America/New_York"


async def test_client_agent_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/schedule/availability", json=_VALID_BODY, cookies={"access_token": token}
        )

    assert response.status_code == 403


async def test_visitor_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.put(
            "/admin/schedule/availability", json=_VALID_BODY, cookies={"access_token": token}
        )

    assert response.status_code == 403


async def test_no_auth_returns_401() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/admin/schedule/availability", json=_VALID_BODY)

    assert response.status_code == 401


async def test_invalid_timezone_returns_422_and_nothing_persisted() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body = {**_VALID_BODY, "timezone": "Not/A_Real_Zone"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/schedule/availability", json=body, cookies={"access_token": token}
        )

    assert response.status_code == 422
    assert db._availability == {}


@pytest.mark.parametrize(
    "rules_override",
    [
        pytest.param({"slot_minutes": 0}, id="slot_minutes_zero"),
        pytest.param({"slot_minutes": -5}, id="slot_minutes_negative"),
        pytest.param({"buffer_minutes": -1}, id="buffer_minutes_negative"),
        pytest.param(
            {"weekly_hours": {"mon": [["9:00", "17:00"]]}},
            id="bad_hhmm_format",
        ),
        pytest.param(
            {"weekly_hours": {"mon": [["17:00", "09:00"]]}},
            id="start_after_end",
        ),
        pytest.param(
            {"weekly_hours": {"monday": [["09:00", "17:00"]]}},
            id="unknown_weekday_key",
        ),
    ],
)
async def test_invalid_rules_shape_returns_422(rules_override: dict[str, Any]) -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body = {
        "timezone": "America/New_York",
        "rules": {**_VALID_BODY["rules"], **rules_override},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/schedule/availability", json=body, cookies={"access_token": token}
        )

    assert response.status_code == 422
    assert db._availability == {}


async def test_tenant_scoped_no_cross_tenant_collision() -> None:
    db = _StubDatabase()
    app = _build_app(db)

    body_b = {**_VALID_BODY, "timezone": "UTC"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token_a = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        token_b = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        await client.put(
            "/admin/schedule/availability", json=_VALID_BODY, cookies={"access_token": token_a}
        )
        await client.put(
            "/admin/schedule/availability", json=body_b, cookies={"access_token": token_b}
        )

    assert db._availability[_TENANT_ID]["timezone"] == "America/New_York"
    assert db._availability[_OTHER_TENANT_ID]["timezone"] == "UTC"


# ==============================================================================
# GET /admin/schedule/calendar/google/authorize
# ==============================================================================


async def test_google_authorize_client_admin_returns_url_and_issues_state() -> None:
    db = _StubDatabase()
    redis = _RecordingRedis()
    app = _build_app(db, redis=redis, extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/schedule/calendar/google/authorize", cookies={"access_token": token}
        )

    assert response.status_code == 200
    data = response.json()
    parsed = urlparse(data["authorize_url"])
    assert parsed.netloc == "accounts.google.com"
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["platform-client-id"]
    assert params["redirect_uri"] == [_GOOGLE_OAUTH_ENV["GOOGLE_OAUTH_REDIRECT_URI"]]

    state_keys = [k for k, _, _ in redis.set_calls if k.startswith("scheduling:google_oauth_state:")]
    assert len(state_keys) == 1
    # The raw state value in the returned URL must never appear as the
    # storage key -- only its hash does (google_oauth_state.py).
    assert params["state"][0] not in state_keys[0]


async def test_google_authorize_client_agent_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis(), extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get(
            "/admin/schedule/calendar/google/authorize", cookies={"access_token": token}
        )

    assert response.status_code == 403


async def test_google_authorize_visitor_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis(), extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get(
            "/admin/schedule/calendar/google/authorize", cookies={"access_token": token}
        )

    assert response.status_code == 403


async def test_google_authorize_no_auth_returns_401() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis(), extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/schedule/calendar/google/authorize")

    assert response.status_code == 401


async def test_google_authorize_not_configured_returns_422() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis())  # no _GOOGLE_OAUTH_ENV

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/schedule/calendar/google/authorize", cookies={"access_token": token}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "GOOGLE_OAUTH_NOT_CONFIGURED"


# ==============================================================================
# GET /admin/schedule/calendar/google/callback
# ==============================================================================


async def _issue_state(app: Any, *, tenant_id: str) -> str:
    """Drive the real authorize route to get a validly-issued state token."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=tenant_id)
        response = await client.get(
            "/admin/schedule/calendar/google/authorize", cookies={"access_token": token}
        )
    parsed = urlparse(response.json()["authorize_url"])
    return parse_qs(parsed.query)["state"][0]


async def test_google_callback_success_stores_config_and_redirects_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.scheduling.admin_routes as admin_routes_mod
    from api.scheduling.google_oauth import GoogleTokens

    # Fixture values kept out of bare `access_token="..."`/`refresh_token="..."`
    # kwargs so they read clearly as test data, not real credentials.
    minted_access_value = "minted-access-token"
    minted_refresh_value = "minted-refresh-token"

    async def _fake_exchange(**kwargs: Any) -> GoogleTokens:
        return GoogleTokens(
            access_token=minted_access_value, refresh_token=minted_refresh_value, expires_in=3599
        )

    monkeypatch.setattr(admin_routes_mod, "exchange_google_auth_code", _fake_exchange)

    db = _StubDatabase()
    redis = _RecordingRedis()
    app = _build_app(db, redis=redis, extra_env=_GOOGLE_OAUTH_ENV)

    state = await _issue_state(app, tenant_id=_TENANT_ID)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"code": "auth-code-1", "state": state},
            cookies={"access_token": token},
        )

    assert response.status_code in (302, 307)
    location = response.headers["location"]
    assert location == "https://admin.example.com/workspace?calendar_connected=true"
    assert db.calendar_configs[_TENANT_ID]["provider"] == "google"
    assert db.calendar_configs[_TENANT_ID]["enabled"] is True
    # The refresh token is stored ENCRYPTED, never in plaintext.
    assert minted_refresh_value not in str(db.calendar_configs[_TENANT_ID]["credentials_ciphertext"])


async def test_google_callback_access_denied_redirects_with_error() -> None:
    db = _StubDatabase()
    redis = _RecordingRedis()
    app = _build_app(db, redis=redis, extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"error": "access_denied"},
            cookies={"access_token": token},
        )

    assert response.headers["location"].endswith("?calendar_error=access_denied")
    assert db.calendar_configs == {}


async def test_google_callback_missing_code_or_state_redirects_with_error() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis(), extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/schedule/calendar/google/callback", cookies={"access_token": token}
        )

    assert response.headers["location"].endswith("?calendar_error=missing_code_or_state")


async def test_google_callback_invalid_state_redirects_with_error() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis(), extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"code": "auth-code-1", "state": "never-issued-state"},
            cookies={"access_token": token},
        )

    assert response.headers["location"].endswith("?calendar_error=invalid_state")
    assert db.calendar_configs == {}


async def test_google_callback_state_from_other_tenant_rejected() -> None:
    """A state issued for tenant A can never be completed while
    authenticated as tenant B -- the RBAC/isolation-critical check."""
    db = _StubDatabase()
    redis = _RecordingRedis()
    app = _build_app(db, redis=redis, extra_env=_GOOGLE_OAUTH_ENV)

    state_for_tenant_a = await _issue_state(app, tenant_id=_TENANT_ID)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        token_b = _token(Role.CLIENT_ADMIN, tenant_id=_OTHER_TENANT_ID)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"code": "auth-code-1", "state": state_for_tenant_a},
            cookies={"access_token": token_b},
        )

    assert response.headers["location"].endswith("?calendar_error=invalid_state")
    assert db.calendar_configs == {}


async def test_google_callback_not_configured_redirects_with_error() -> None:
    """OAuth becomes unconfigured between authorize and callback (e.g. env
    misconfiguration) -- must fail loud via the redirect, not store a
    dangling config."""
    db = _StubDatabase()
    redis = _RecordingRedis()
    app_configured = _build_app(db, redis=redis, extra_env=_GOOGLE_OAUTH_ENV)
    state = await _issue_state(app_configured, tenant_id=_TENANT_ID)

    _reset_settings()
    app_unconfigured = _build_app(db, redis=redis)

    async with AsyncClient(
        transport=ASGITransport(app=app_unconfigured), base_url="http://test", follow_redirects=False
    ) as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"code": "auth-code-1", "state": state},
            cookies={"access_token": token},
        )

    assert response.headers["location"].endswith("?calendar_error=not_configured")
    assert db.calendar_configs == {}


async def test_google_callback_exchange_failed_redirects_with_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.scheduling.admin_routes as admin_routes_mod
    from api.scheduling.google_oauth import GoogleOAuthError

    async def _fake_exchange_failure(**kwargs: Any) -> Any:
        raise GoogleOAuthError("token exchange failed")

    monkeypatch.setattr(admin_routes_mod, "exchange_google_auth_code", _fake_exchange_failure)

    db = _StubDatabase()
    redis = _RecordingRedis()
    app = _build_app(db, redis=redis, extra_env=_GOOGLE_OAUTH_ENV)

    state = await _issue_state(app, tenant_id=_TENANT_ID)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"code": "auth-code-1", "state": state},
            cookies={"access_token": token},
        )

    assert response.headers["location"].endswith("?calendar_error=exchange_failed")
    assert db.calendar_configs == {}


async def test_google_callback_client_agent_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis(), extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"code": "auth-code-1", "state": "whatever"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_google_callback_visitor_forbidden() -> None:
    db = _StubDatabase()
    app = _build_app(db, redis=_RecordingRedis(), extra_env=_GOOGLE_OAUTH_ENV)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get(
            "/admin/schedule/calendar/google/callback",
            params={"code": "auth-code-1", "state": "whatever"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403
