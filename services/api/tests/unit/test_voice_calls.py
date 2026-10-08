"""AI voice confirmation call (api.calls.voice*): helpers, booking hook,
Celery placement against a mocked Twilio, signed webhooks, admin routes."""
from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.calls.voice import (
    TwilioCredentials,
    goodbye_twiml,
    parse_yes_no,
    question_twiml,
    render_questions,
    schedule_booking_call,
    to_e164,
)
from api.calls.voice_repository import VoiceCall, VoiceCallConfig
from api.calls.voice_tasks import TwilioTransientError, place_call

_TENANT = "tenant-a"
_CALL_ID = "call-1"
_BASE = "https://api.example.test"
_TOKEN = "twilio" + "-" + "test" + "-" + "token"  # not a real credential
_CREDS = TwilioCredentials("ACtest", _TOKEN, "+15005550006")

_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
    "PUBLIC_API_BASE_URL": _BASE,
}


def _reset_settings() -> None:
    from common.settings import get_settings

    import api.calls.voice as voice_module
    import api.calls.voice_webhook as webhook_module
    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()
    # Another test file drops `api.config` from sys.modules, so these modules
    # can hold a different (older) get_api_settings than a fresh import --
    # clear the copies they actually call.
    voice_module.get_api_settings.cache_clear()
    webhook_module.get_api_settings.cache_clear()


@pytest.fixture
def env() -> Iterator[None]:
    with patch.dict("os.environ", _ENV, clear=False):
        _reset_settings()
        yield
    _reset_settings()


def _call(**overrides: Any) -> VoiceCall:
    fields: dict[str, Any] = {
        "call_id": _CALL_ID,
        "tenant_id": _TENANT,
        "event_id": "evt-1",
        "lead_id": "lead-1",
        "to_number": "+15551234567",
        "questions": ["Can you make it?", "Is it leaking?"],
        "status": "queued",
        "twilio_call_sid": None,
        "transcript": [],
        "last_error": None,
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return VoiceCall(**fields)


def _claims() -> AuthClaims:
    return AuthClaims(subject="visitor-1", role=Role.VISITOR, tenant_id=_TENANT)


# -- pure helpers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(555) 123-4567", "+15551234567"),
        ("1 555 123 4567", "+15551234567"),
        ("+44 20 7946 0958", "+442079460958"),
        ("12345", None),
        ("", None),
    ],
)
def test_to_e164(raw: str, expected: str | None) -> None:
    assert to_e164(raw) == expected


def test_render_questions_fills_date_and_time_in_the_booking_timezone() -> None:
    starts = datetime(2026, 10, 5, 18, 30, tzinfo=UTC)  # 2:30 PM EDT
    [q] = render_questions(["Free on {date} at {time}?"], starts, "America/New_York")
    assert q == "Free on Monday, October 5 at 2:30 PM?"


@pytest.mark.parametrize(
    ("speech", "digits", "expected"),
    [
        (None, "1", "yes"),
        (None, "2", "no"),
        (None, "9", "unclear"),
        ("Yes, that works.", None, "yes"),
        ("nope", None, "no"),
        ("I'm not sure", None, "unclear"),
        ("", None, "no_response"),
        (None, None, "no_response"),
    ],
)
def test_parse_yes_no(speech: str | None, digits: str | None, expected: str) -> None:
    assert parse_yes_no(speech, digits) == expected


def test_twiml_escapes_question_text_and_hangs_up_at_the_end() -> None:
    xml = question_twiml(question="Fish & <chips>?", action_url="https://x/a?b=1&c=2",
                         voice="Polly.Joanna-Neural", intro=True)
    assert "Fish &amp; &lt;chips&gt;?" in xml
    assert 'action="https://x/a?b=1&amp;c=2"' in xml
    assert 'actionOnEmptyResult="true"' in xml
    assert "<Hangup/>" in goodbye_twiml("Polly.Joanna-Neural")


# -- booking hook ---------------------------------------------------------------------


async def _schedule(config: VoiceCallConfig | None, phone: str | None) -> tuple[Any, AsyncMock]:
    create = AsyncMock(return_value=_CALL_ID)
    with (
        patch("api.calls.voice.get_voice_call_config", AsyncMock(return_value=config)),
        patch("api.calls.voice.create_voice_call", create),
    ):
        result = await schedule_booking_call(
            object(), _claims(), event_id="evt-1", lead_id="lead-1", phone=phone,  # type: ignore[arg-type]
            starts_at=datetime(2026, 10, 5, 18, 30, tzinfo=UTC), timezone="America/New_York",
        )
    return result, create


async def test_booking_call_created_with_rendered_questions_and_e164_number() -> None:
    config = VoiceCallConfig(enabled=True, questions=["See you {date} at {time}?"])
    result, create = await _schedule(config, "(555) 123-4567")

    assert result == _CALL_ID
    kwargs = create.await_args.kwargs
    assert kwargs["to_number"] == "+15551234567"
    assert kwargs["questions"] == ["See you Monday, October 5 at 2:30 PM?"]


@pytest.mark.parametrize(
    ("config", "phone"),
    [
        (None, "(555) 123-4567"),
        (VoiceCallConfig(enabled=False, questions=["q"]), "(555) 123-4567"),
        (VoiceCallConfig(enabled=True, questions=["q"]), None),
        (VoiceCallConfig(enabled=True, questions=["q"]), "123"),
    ],
)
async def test_no_call_when_off_or_no_diallable_phone(config: Any, phone: str | None) -> None:
    result, create = await _schedule(config, phone)
    assert result is None
    create.assert_not_awaited()


# -- Celery placement (Twilio mocked) -------------------------------------------------


async def _place(
    env_handler: Any, *, call: VoiceCall | None, creds: TwilioCredentials | None = _CREDS,
) -> tuple[dict[str, object], AsyncMock, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return env_handler(request)

    real_client = httpx.AsyncClient
    set_status = AsyncMock()
    with (
        patch("api.calls.voice_tasks.get_voice_call_by_id", AsyncMock(return_value=call)),
        patch("api.calls.voice_tasks.set_voice_call_status", set_status),
        patch("api.calls.voice_tasks.twilio_credentials_for", AsyncMock(return_value=creds)),
        patch(
            "api.calls.voice_tasks.httpx.AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        ),
    ):
        result = await place_call(object(), call_id=_CALL_ID, tenant_id=_TENANT)  # type: ignore[arg-type]
    return result, set_status, seen


async def test_place_call_dials_twilio_and_marks_calling(env: None) -> None:
    result, set_status, seen = await _place(
        lambda _r: httpx.Response(201, json={"sid": "CA123"}), call=_call(),
    )

    assert result["status"] == "calling"
    [request] = seen
    assert str(request.url) == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Calls.json"
    form = parse_qs(request.content.decode())
    assert form["To"] == ["+15551234567"]
    assert form["From"] == ["+15005550006"]
    assert form["Url"] == [f"{_BASE}/public/calls/voice/{_TENANT}/{_CALL_ID}/start"]
    assert form["StatusCallback"] == [f"{_BASE}/public/calls/voice/{_TENANT}/{_CALL_ID}/status"]
    assert set_status.await_args.kwargs == {"status": "calling", "twilio_call_sid": "CA123"}


async def test_place_call_is_a_no_op_unless_queued(env: None) -> None:
    result, set_status, seen = await _place(lambda _r: httpx.Response(201), call=_call(status="calling"))
    assert result["status"] == "no_op"
    assert seen == []
    set_status.assert_not_awaited()


async def test_place_call_without_twilio_is_recorded_as_failed(env: None) -> None:
    result, set_status, seen = await _place(lambda _r: httpx.Response(201), call=_call(), creds=None)
    assert result["status"] == "failed"
    assert seen == []
    assert set_status.await_args.kwargs == {"status": "failed", "last_error": "TWILIO_NOT_CONFIGURED"}


async def test_place_call_twilio_4xx_fails_with_twilio_reason(env: None) -> None:
    result, set_status, _ = await _place(
        lambda _r: httpx.Response(400, json={"code": 21219, "message": "unverified number"}),
        call=_call(),
    )
    assert result["status"] == "failed"
    assert set_status.await_args.kwargs["last_error"] == "TWILIO_21219: unverified number"


async def test_place_call_twilio_5xx_raises_for_retry(env: None) -> None:
    with pytest.raises(TwilioTransientError):
        await _place(lambda _r: httpx.Response(503), call=_call())


# -- signed webhooks ------------------------------------------------------------------


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


def _app() -> Any:
    from api.app import create_app

    app = create_app()
    app.state.db = object()
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _sign(url: str, params: dict[str, str], token: str = _TOKEN) -> str:
    signed = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode(), signed.encode(), hashlib.sha1).digest()
    ).decode()


async def _post_hook(
    path: str, params: dict[str, str], *, call: VoiceCall | None = None, token: str = _TOKEN,
) -> tuple[httpx.Response, AsyncMock, AsyncMock]:
    record = AsyncMock()
    set_status = AsyncMock()
    url = f"{_BASE}/public/calls/voice/{_TENANT}/{_CALL_ID}{path}"
    with (
        patch("api.calls.voice_webhook.twilio_credentials_for", AsyncMock(return_value=_CREDS)),
        patch("api.calls.voice_webhook.get_voice_call_by_id", AsyncMock(return_value=call or _call())),
        patch("api.calls.voice_webhook.record_voice_call_answer", record),
        patch("api.calls.voice_webhook.set_voice_call_status", set_status),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://api_1:8000") as c:
            response = await c.post(
                f"/public/calls/voice/{_TENANT}/{_CALL_ID}{path}",
                content=urlencode(params),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Twilio-Signature": _sign(url, params, token),
                },
            )
    return response, record, set_status


async def test_start_speaks_the_intro_and_first_question(env: None) -> None:
    response, _, _ = await _post_hook("/start", {"CallSid": "CA1"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "automated call" in response.text
    assert "Can you make it?" in response.text
    assert f"{_BASE}/public/calls/voice/{_TENANT}/{_CALL_ID}/answer/0" in response.text


async def test_answer_is_recorded_and_the_next_question_follows(env: None) -> None:
    response, record, _ = await _post_hook("/answer/0", {"CallSid": "CA1", "SpeechResult": "Yes I can"})

    assert record.await_args.kwargs == {
        "index": 0,
        "entry": {"question": "Can you make it?", "heard": "Yes I can", "answer": "yes"},
    }
    assert "Is it leaking?" in response.text
    assert "automated call" not in response.text


async def test_last_answer_says_goodbye_and_hangs_up(env: None) -> None:
    response, record, _ = await _post_hook("/answer/1", {"CallSid": "CA1", "Digits": "2"})

    assert record.await_args.kwargs["entry"]["answer"] == "no"
    assert "<Hangup/>" in response.text


async def test_unclear_answer_is_recorded_and_the_call_moves_on(env: None) -> None:
    response, record, _ = await _post_hook("/answer/0", {"CallSid": "CA1", "SpeechResult": "hmm"})

    assert record.await_args.kwargs["entry"]["answer"] == "unclear"
    assert "Is it leaking?" in response.text  # never re-asks question 0


async def test_bad_signature_is_rejected_and_nothing_recorded(env: None) -> None:
    response, record, _ = await _post_hook(
        "/answer/0", {"SpeechResult": "yes"}, token="wrong" + "-" + "token",
    )
    assert response.status_code == 401
    record.assert_not_awaited()


async def test_status_callback_records_the_terminal_status(env: None) -> None:
    response, _, set_status = await _post_hook("/status", {"CallSid": "CA1", "CallStatus": "no-answer"})

    assert response.status_code == 200
    assert set_status.await_args.kwargs == {"status": "no_answer", "twilio_call_sid": "CA1"}


# -- admin routes ---------------------------------------------------------------------


def _cookie(role: Role) -> dict[str, str]:
    from api.auth.tokens import create_access_token

    token, _ = create_access_token(
        AuthClaims(subject="admin-1", role=role, tenant_id=_TENANT), secret="x" * 48, ttl_seconds=300,
    )
    return {"access_token": token}


async def test_voice_config_defaults_to_off_with_default_questions(env: None) -> None:
    with patch("api.calls.voice_admin_routes.get_voice_call_config", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
            response = await c.get("/admin/calls/voice-config", cookies=_cookie(Role.CLIENT_ADMIN))

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert len(body["questions"]) == 3


async def test_voice_config_rejects_an_empty_question_list(env: None) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
        response = await c.put(
            "/admin/calls/voice-config",
            json={"enabled": True, "questions": ["  ", ""]},
            cookies=_cookie(Role.CLIENT_ADMIN),
        )
    assert response.status_code == 422


async def test_agents_cannot_change_voice_config(env: None) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
        response = await c.put(
            "/admin/calls/voice-config",
            json={"enabled": True, "questions": ["q"]},
            cookies=_cookie(Role.CLIENT_AGENT),
        )
    assert response.status_code == 403


async def test_lead_voice_calls_list_transcripts_for_agents(env: None) -> None:
    call = _call(
        status="completed",
        transcript=[{"question": "Can you make it?", "heard": "yes", "answer": "yes"}],
    )
    lister = AsyncMock(return_value=[call])
    with patch("api.calls.voice_admin_routes.list_voice_calls_for_lead", lister):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
            response = await c.get(
                "/admin/calls/leads/lead-1/voice-calls", cookies=_cookie(Role.CLIENT_AGENT),
            )

    assert response.status_code == 200
    [item] = response.json()
    assert item["status"] == "completed"
    assert item["transcript"] == [{"question": "Can you make it?", "heard": "yes", "answer": "yes"}]
    assert lister.await_args.args[1].tenant_id == _TENANT
