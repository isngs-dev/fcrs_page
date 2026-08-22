"""Unit tests for widget admission (POST /widget/session, GET /widget/whoami).

Covers:
- POST /widget/session: valid key + allowed Origin -> 200; unknown key -> 422;
  disallowed/missing Origin -> 403; disabled tenant -> 403.
- Multi-tenant isolation: key for tenant A + A's origin -> token carries A's tenant_id.
- GET /widget/whoami: valid visitor bearer -> 200; no header -> 401;
  non-visitor token -> 403; tampered/expired bearer -> 401.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.admin.repository import _hash_client_key
from api.auth.tokens import create_access_token

# -- Constants -----------------------------------------------------------------

_TEST_JWT_SECRET = "x" * 48
_TENANT_A_ID = "tenant-a-123"
_TENANT_B_ID = "tenant-b-456"
_CLIENT_KEY_A = "pk_key-aaa"
_CLIENT_KEY_B = "pk_key-bbb"

# -- Test doubles --------------------------------------------------------------

_TENANT_A_ROW: dict[str, Any] = {
    "id": _TENANT_A_ID,
    "slug": "tenant-a",
    "enabled": True,
    "allowed_origins": ["http://localhost:3000", "https://a.example.com"],
}

_TENANT_B_ROW: dict[str, Any] = {
    "id": _TENANT_B_ID,
    "slug": "tenant-b",
    "enabled": True,
    "allowed_origins": ["https://b.example.com"],
}

_TENANT_DISABLED_ROW: dict[str, Any] = {
    "id": "tenant-disabled",
    "slug": "tenant-disabled",
    "enabled": False,
    "allowed_origins": ["http://localhost:3000"],
}

# S12.1: tenants.client_key_hash stores a SHA-256 hash, not the raw key.
# get_tenant_by_client_key hashes the incoming raw key before looking it up,
# so this stub DB (and the SQL it receives) must be keyed by hash too --
# proves the hashing migration is a genuine behavior change, not dead code.
_CLIENT_KEY_DB: dict[str, dict[str, Any]] = {
    _hash_client_key(_CLIENT_KEY_A): _TENANT_A_ROW,
    _hash_client_key(_CLIENT_KEY_B): _TENANT_B_ROW,
    _hash_client_key("pk_disabled"): _TENANT_DISABLED_ROW,
}

# SR-3: tenant_bot_settings.business_hours JSON keyed by tenant_id -- the
# migration-free home of the widget_session_resume opt-in flag (decision 8/9).
# Populated per-test via `_StubDatabase(bot_settings=...)`.


class _StubDatabase:
    """Database double for gateway queries (lookup by client_key_hash)."""

    def __init__(self, *, bot_settings: dict[str, dict[str, Any]] | None = None) -> None:
        # tenant_id -> tenant_bot_settings row (SR-3: only business_hours matters here).
        self._bot_settings = bot_settings or {}

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        if "tenant_bot_settings" in query:
            tenant_id = str(args[0]) if args else None
            row = self._bot_settings.get(tenant_id) if tenant_id else None
            return (
                {
                    "business_hours": row.get("business_hours"),
                    "launcher_label": row.get("launcher_label"),
                }
                if row is not None
                else None
            )
        if args:
            # The raw client key must NEVER be the bound param -- only its hash.
            bound = str(args[0])
            assert bound not in (  # noqa: S101
                _CLIENT_KEY_A,
                _CLIENT_KEY_B,
                "pk_disabled",
            ), "raw client_key leaked into SQL binding -- expected only the hash"
            return _CLIENT_KEY_DB.get(bound)
        return None

    async def fetchval(self, query: str, *args: object) -> object:
        return 1

    async def execute(self, query: str, *args: object) -> str:
        return "UPDATE 1"

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


# -- Helpers -------------------------------------------------------------------

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


def _build_app(*, bot_settings: dict[str, dict[str, Any]] | None = None) -> Any:
    """Create app with test doubles."""
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app
        app = create_app()

    app.state.db = _StubDatabase(bot_settings=bot_settings)
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _mint_visitor_token(
    *,
    tenant_id: str = _TENANT_A_ID,
    role: Role = Role.VISITOR,
    ttl_seconds: int = 300,
    secret: str = _TEST_JWT_SECRET,
) -> str:
    """Create a signed JWT for use as a bearer token."""
    claims = AuthClaims(
        subject="visitor-1",
        role=role,
        tenant_id=tenant_id,
    )
    token, _ = create_access_token(claims, secret=secret, ttl_seconds=ttl_seconds)
    return token


# ==============================================================================
# POST /widget/session
# ==============================================================================


async def test_widget_session_valid_key_and_origin() -> None:
    """Valid key + allowed Origin -> 200 with visitor_token + expires_at."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "visitor_token" in body
    assert "expires_at" in body

    # Decode the token to verify role and tenant_id
    from api.auth.tokens import decode_access_token
    payload = decode_access_token(body["visitor_token"], secret=_TEST_JWT_SECRET)
    assert payload["role"] == "VISITOR"
    assert payload["tenant_id"] == _TENANT_A_ID


async def test_widget_session_unknown_key() -> None:
    """Unknown client key -> 422 INVALID_CLIENT_KEY."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": "pk_does_not_exist"},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_CLIENT_KEY"


async def test_widget_session_disallowed_origin() -> None:
    """Valid key + disallowed Origin -> 403 ORIGIN_NOT_ALLOWED."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://evil.example"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ORIGIN_NOT_ALLOWED"


async def test_widget_session_missing_origin() -> None:
    """Valid key + no Origin header -> 403 ORIGIN_NOT_ALLOWED."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ORIGIN_NOT_ALLOWED"


async def test_widget_session_disabled_tenant() -> None:
    """Valid key + disabled tenant -> 403 TENANT_DISABLED."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": "pk_disabled"},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "TENANT_DISABLED"


# ==============================================================================
# Multi-tenant isolation
# ==============================================================================


async def test_widget_session_multi_tenant_isolation() -> None:
    """Key for tenant A + A's origin -> token carries A's tenant_id, never B's."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200
    body = resp.json()

    from api.auth.tokens import decode_access_token
    payload = decode_access_token(body["visitor_token"], secret=_TEST_JWT_SECRET)
    assert payload["tenant_id"] == _TENANT_A_ID
    assert payload["tenant_id"] != _TENANT_B_ID


async def test_widget_session_wrong_origin_for_tenant() -> None:
    """Tenant B's key + tenant A's origin -> 403 (A's origin not in B's allowlist)."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_B},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "ORIGIN_NOT_ALLOWED"


# ==============================================================================
# GET /widget/whoami
# ==============================================================================


async def test_widget_whoami_valid_visitor_bearer() -> None:
    """Valid visitor bearer -> 200 with visitor_id, tenant_id, role=VISITOR."""
    app = _build_app()
    token = _mint_visitor_token(tenant_id=_TENANT_A_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/widget/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "VISITOR"
    assert body["tenant_id"] == _TENANT_A_ID
    assert body["visitor_id"] == "visitor-1"


async def test_widget_whoami_no_authorization_header() -> None:
    """No Authorization header -> 401 UNAUTHENTICATED."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/widget/whoami")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"


async def test_widget_whoami_non_visitor_token() -> None:
    """CLIENT_ADMIN token as bearer -> 403 NOT_A_VISITOR."""
    app = _build_app()
    token = _mint_visitor_token(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/widget/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "NOT_A_VISITOR"


async def test_widget_whoami_tampered_token() -> None:
    """Tampered bearer -> 401 UNAUTHENTICATED."""
    app = _build_app()
    token = _mint_visitor_token()
    tampered = token[:-4] + "XXXX"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/widget/whoami",
            headers={"Authorization": f"Bearer {tampered}"},
        )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"


async def test_widget_session_valid_key_and_origin_default_tenant_resume_disabled() -> None:
    """SR-3: a tenant with no tenant_bot_settings row -> resume_enabled=False,
    the S14.1/S14.2 default-off posture (decision 8). No other field changes."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resume_enabled"] is False
    assert set(body.keys()) == {
        "visitor_token",
        "expires_at",
        "resume_enabled",
        "voice_asr_enabled",
        "voice_tts_enabled",
    }


async def test_widget_session_voice_flags_false_when_no_provider_keys_configured() -> None:
    """Real test settings carry no OPENAI_ASR_API_KEY/ELEVENLABS_API_KEY --
    both flags are false, platform-global (not per-tenant) booleans."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["voice_asr_enabled"] is False
    assert body["voice_tts_enabled"] is False


async def test_widget_session_voice_flags_true_when_provider_keys_configured() -> None:
    """Setting both env vars flips both flags true -- proves the route reads
    live settings, not a hardcoded false."""
    from unittest.mock import patch

    app = _build_app()
    with patch("api.gateway.routes.get_api_settings") as mock_settings:
        from api.config import ApiSettings

        real = ApiSettings()  # type: ignore[call-arg]
        real.openai_asr_api_key = "sk-fake"
        real.elevenlabs_api_key = "sk-fake"
        real.elevenlabs_voice_id = "voice-123"
        mock_settings.return_value = real

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/widget/session",
                json={"client_key": _CLIENT_KEY_A},
                headers={"Origin": "http://localhost:3000"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["voice_asr_enabled"] is True
    assert body["voice_tts_enabled"] is True


async def test_widget_session_resume_enabled_tenant_echoes_true() -> None:
    """SR-3: a tenant whose tenant_bot_settings.business_hours JSON carries
    widget_session_resume=true -> resume_enabled=True in the admission
    response (decision 8/9). mint_visitor_session is otherwise unchanged."""
    app = _build_app(
        bot_settings={_TENANT_A_ID: {"business_hours": {"widget_session_resume": True}}},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resume_enabled"] is True

    from api.auth.tokens import decode_access_token
    payload = decode_access_token(body["visitor_token"], secret=_TEST_JWT_SECRET)
    assert payload["role"] == "VISITOR"
    assert payload["tenant_id"] == _TENANT_A_ID


async def test_widget_session_echoes_only_the_resolved_tenants_launcher_label() -> None:
    app = _build_app(
        bot_settings={
            _TENANT_A_ID: {"launcher_label": "Chat with A"},
            _TENANT_B_ID: {"launcher_label": "Chat with B"},
        },
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["launcher_label"] == "Chat with A"
    assert body["launcher_label"] != "Chat with B"
    assert "tenant_id" not in body
    assert "visitor_id" not in body


async def test_widget_session_omits_launcher_label_when_the_tenant_has_none() -> None:
    app = _build_app(bot_settings={_TENANT_A_ID: {"launcher_label": None}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )

    assert response.status_code == 200
    assert "launcher_label" not in response.json()


async def test_launcher_label_repository_query_is_tenant_scoped_and_parameterized() -> None:
    """The admission echo may only read the resolved tenant's row."""
    from api.gateway.repository import get_launcher_label

    class _RecordingDatabase:
        query: str = ""
        args: tuple[object, ...] = ()

        async def fetchrow(self, query: str, *args: object) -> dict[str, str]:
            self.query = query
            self.args = args
            return {"launcher_label": "Chat with A"}

    db = _RecordingDatabase()
    assert await get_launcher_label(db, _TENANT_A_ID) == "Chat with A"
    assert "WHERE tenant_id = $1" in db.query
    assert db.args == (_TENANT_A_ID,)


async def test_widget_session_resume_flag_false_when_business_hours_missing_key() -> None:
    """A tenant_bot_settings row exists but its business_hours JSON does not
    carry widget_session_resume -> defaults False (never a KeyError, never a
    silent true)."""
    app = _build_app(
        bot_settings={_TENANT_A_ID: {"business_hours": {"mon": [["09:00", "17:00"]]}}},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200
    assert resp.json()["resume_enabled"] is False


async def test_widget_session_resume_flag_false_when_business_hours_null() -> None:
    """A tenant_bot_settings row with business_hours=NULL -> resume_enabled
    defaults False, never raises."""
    app = _build_app(bot_settings={_TENANT_A_ID: {"business_hours": None}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/widget/session",
            json={"client_key": _CLIENT_KEY_A},
            headers={"Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200
    assert resp.json()["resume_enabled"] is False


async def test_widget_whoami_expired_token() -> None:
    """Expired bearer -> 401 UNAUTHENTICATED."""
    app = _build_app()
    past = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=2)
    with patch("api.auth.tokens._dt") as mock_dt:
        mock_dt.UTC = _dt.UTC
        mock_dt.datetime.now.return_value = past
        mock_dt.timedelta = _dt.timedelta
        token = _mint_visitor_token(ttl_seconds=60)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/widget/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"
