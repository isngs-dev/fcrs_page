"""Unit tests for POST /public/chat/transcribe and POST /public/chat/speak.

Covers:
- No/malformed Authorization -> 401.
- Non-visitor token -> 403 NOT_A_VISITOR.
- Neither provider configured (real test settings, no keys) -> 422
  VOICE_PROVIDER_NOT_CONFIGURED for both endpoints.
- Happy path (patched provider) -> 200 {text} for transcribe, 200 raw
  audio/mpeg bytes for speak.
- transcribe: oversized audio -> 422 AUDIO_TOO_LARGE; empty audio -> 422
  AUDIO_EMPTY.
- speak: blank text -> 422.
- Upstream VoiceProviderError -> 502 for both endpoints.
- Neither response ever leaks tenant_id/visitor_id.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token
from api.voice.provider import VoiceProviderError

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"

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


class _StubDatabase:
    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        return None

    async def execute(self, query: str, *args: object) -> str:
        return "INSERT 1"

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


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app() -> Any:
    _reset_settings()
    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()
        app.state.db = _StubDatabase()
        app.state.redis = _StubRedis()
        app.state.cache = InMemoryCache()
        return app


def _visitor_token(tenant_id: str = _TENANT_ID, visitor_id: str = "visitor-123") -> str:
    claims = AuthClaims(subject=visitor_id, role=Role.VISITOR, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


def _admin_token(tenant_id: str = _TENANT_ID) -> str:
    claims = AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


class _StubASRProvider:
    def __init__(self, *, text: str = "hello there", raise_error: Exception | None = None) -> None:
        self._text = text
        self._raise_error = raise_error
        self.closed = False

    async def transcribe(self, audio: bytes, *, content_type: str) -> str:
        if self._raise_error is not None:
            raise self._raise_error
        return self._text

    async def aclose(self) -> None:
        self.closed = True


class _StubTTSProvider:
    def __init__(self, *, audio: bytes = b"fake-mp3-bytes", raise_error: Exception | None = None) -> None:
        self._audio = audio
        self._raise_error = raise_error
        self.received_text: str | None = None

    async def synthesize(self, text: str) -> bytes:
        self.received_text = text
        if self._raise_error is not None:
            raise self._raise_error
        return self._audio


# ==============================================================================
# POST /public/chat/transcribe
# ==============================================================================


async def test_transcribe_no_auth_header_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/public/chat/transcribe", content=b"fake-audio")
    assert resp.status_code == 401


async def test_transcribe_non_visitor_token_403() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/public/chat/transcribe",
            content=b"fake-audio",
            headers={"Authorization": f"Bearer {_admin_token()}"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "NOT_A_VISITOR"


async def test_transcribe_not_configured_422() -> None:
    """Real test settings carry no OPENAI_API_KEY -- a deterministic 422,
    never a silent fallback or a crash."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/public/chat/transcribe",
            content=b"fake-audio-bytes",
            headers={
                "Authorization": f"Bearer {_visitor_token()}",
                "Content-Type": "audio/webm",
            },
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VOICE_PROVIDER_NOT_CONFIGURED"


async def test_transcribe_happy_path_returns_text_leak_free() -> None:
    app = _build_app()
    stub = _StubASRProvider(text="how much does an inspection cost")

    with patch("api.voice.routes.asr_provider_for", return_value=stub):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/public/chat/transcribe",
                content=b"fake-audio-bytes",
                headers={
                    "Authorization": f"Bearer {_visitor_token()}",
                    "Content-Type": "audio/webm",
                },
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"text": "how much does an inspection cost"}
    assert "tenant_id" not in body
    assert "visitor_id" not in body
    assert stub.closed is True


async def test_transcribe_empty_audio_422() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/public/chat/transcribe",
            content=b"",
            headers={"Authorization": f"Bearer {_visitor_token()}"},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "AUDIO_EMPTY"


async def test_transcribe_oversized_audio_422() -> None:
    app = _build_app()
    with patch("api.voice.routes.get_api_settings") as mock_settings:
        from api.config import ApiSettings

        real = ApiSettings()  # type: ignore[call-arg]
        real.voice_max_audio_upload_bytes = 10
        mock_settings.return_value = real

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/public/chat/transcribe",
                content=b"this payload is definitely more than ten bytes long",
                headers={"Authorization": f"Bearer {_visitor_token()}"},
            )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "AUDIO_TOO_LARGE"


async def test_transcribe_upstream_error_502() -> None:
    app = _build_app()
    stub = _StubASRProvider(raise_error=VoiceProviderError("upstream failed"))

    with patch("api.voice.routes.asr_provider_for", return_value=stub):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/public/chat/transcribe",
                content=b"fake-audio-bytes",
                headers={"Authorization": f"Bearer {_visitor_token()}"},
            )

    assert resp.status_code == 502
    assert resp.json()["error_code"] == "VOICE_PROVIDER_ERROR"
    assert stub.closed is True


# ==============================================================================
# POST /public/chat/speak
# ==============================================================================


async def test_speak_no_auth_header_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/public/chat/speak", json={"text": "hello"})
    assert resp.status_code == 401


async def test_speak_non_visitor_token_403() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/public/chat/speak",
            json={"text": "hello"},
            headers={"Authorization": f"Bearer {_admin_token()}"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "NOT_A_VISITOR"


async def test_speak_blank_text_422() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/public/chat/speak",
            json={"text": "   "},
            headers={"Authorization": f"Bearer {_visitor_token()}"},
        )
    assert resp.status_code == 422


async def test_speak_not_configured_422() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/public/chat/speak",
            json={"text": "We're open Monday through Friday."},
            headers={"Authorization": f"Bearer {_visitor_token()}"},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VOICE_PROVIDER_NOT_CONFIGURED"


async def test_speak_happy_path_returns_audio_mpeg() -> None:
    app = _build_app()
    stub = _StubTTSProvider(audio=b"fake-mp3-bytes")

    with patch("api.voice.routes.tts_provider_for", return_value=stub):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/public/chat/speak",
                json={"text": "We're open Monday through Friday."},
                headers={"Authorization": f"Bearer {_visitor_token()}"},
            )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"fake-mp3-bytes"


async def test_speak_truncates_text_before_it_ever_reaches_the_provider() -> None:
    """A cost ceiling, not a display change: only what's SENT TO the paid
    provider is capped -- the reply bubble itself (POST /public/chat/message)
    is never touched by this route at all."""
    app = _build_app()
    stub = _StubTTSProvider(audio=b"fake-mp3-bytes")
    long_text = "Sentence one. " * 200
    assert len(long_text) > 1200

    with patch("api.voice.routes.tts_provider_for", return_value=stub):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/public/chat/speak",
                json={"text": long_text},
                headers={"Authorization": f"Bearer {_visitor_token()}"},
            )

    assert resp.status_code == 200
    assert stub.received_text is not None
    assert len(stub.received_text) <= 1200
    assert len(stub.received_text) < len(long_text)


async def test_speak_upstream_error_502() -> None:
    app = _build_app()
    stub = _StubTTSProvider(raise_error=VoiceProviderError("upstream failed"))

    with patch("api.voice.routes.tts_provider_for", return_value=stub):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/public/chat/speak",
                json={"text": "We're open Monday through Friday."},
                headers={"Authorization": f"Bearer {_visitor_token()}"},
            )

    assert resp.status_code == 502
    assert resp.json()["error_code"] == "VOICE_PROVIDER_ERROR"
