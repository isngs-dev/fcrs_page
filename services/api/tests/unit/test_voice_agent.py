"""AI voice agent ("Call Us", api.voice_agent): transfer rules, business hours,
call ticket, Plivo signatures + browser JWT, and the Plivo turn webhooks."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import httpx
import pytest
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.voice_agent.agent import (
    AFTER_HOURS_LINE,
    GOODBYE_LINE,
    MISS_LINE,
    TRANSFER_LINE,
    CallSession,
    answer_from_knowledge,
    call_ticket,
    in_business_hours,
    is_repeat,
    verify_call_ticket,
)
from api.voice_agent.plivo import compute_signature, mint_browser_token, verify_signature
from api.voice_agent.repository import VoiceAgentCall, VoiceAgentConfig

_BASE = "https://api.example.test"
_AUTH = "plivo" + "-" + "auth" + "-" + "value"  # not a real credential
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
    "PLATFORM_PLIVO_AUTH_ID": "MAtest",
    "PLATFORM_PLIVO_AUTH_TOKEN": _AUTH,
    "PLATFORM_PLIVO_FROM_NUMBER": "+15005550006",
    "PLATFORM_PLIVO_APP_ID": "1234567890",
    "PLATFORM_PLIVO_ENDPOINT_USERNAME": "voiceagent",
}


def _reset_settings() -> None:
    import sys

    from common.settings import get_settings

    get_settings.cache_clear()
    # Another test file drops `api.config` from sys.modules, so modules may hold
    # an older get_api_settings than a fresh import -- clear every live copy.
    for name in ("api.config", "api.voice_agent.agent", "api.voice_agent.routes"):
        module = sys.modules.get(name)
        if module is not None:
            module.get_api_settings.cache_clear()


@pytest.fixture
def env() -> Iterator[None]:
    with patch.dict("os.environ", _ENV, clear=False):
        _reset_settings()
        yield
    _reset_settings()


def _call(**overrides: Any) -> VoiceAgentCall:
    fields: dict[str, Any] = {
        "call_id": "call1", "tenant_id": "tenant-a", "visitor_id": "visitor-1",
        "conversation_id": "conv-1", "status": "starting", "transfer_reason": None,
        "started_at": None, "ended_at": None, "session": None,
    }
    fields.update(overrides)
    return VoiceAgentCall(**fields)


def _unxml(text: str) -> str:
    return text.replace("&apos;", "'").replace("&#x27;", "'")


# -- transfer rules ---------------------------------------------------------------


def test_asking_for_a_person_transfers_right_away() -> None:
    session = CallSession(max_minutes=10)
    assert session.before_answer("Can I talk to someone from your team?") == (
        "transfer", "asked_for_person",
    )
    assert session.before_answer("I want a real person") == ("transfer", "asked_for_person")


def test_an_ordinary_question_is_answered() -> None:
    assert CallSession(max_minutes=10).before_answer("How much is a roof inspection?") == (
        "answer", None,
    )


def test_goodbye_ends_the_call() -> None:
    assert CallSession(max_minutes=10).before_answer("ok thanks, bye") == ("goodbye", None)


def test_a_question_it_cant_answer_transfers_right_away() -> None:
    session = CallSession(max_minutes=10)
    assert session.after_answer("do you do pools?", None) == (MISS_LINE, "could_not_answer")
    assert MISS_LINE.endswith(TRANSFER_LINE)


def test_an_answered_question_keeps_the_conversation_going() -> None:
    session = CallSession(max_minutes=10)
    assert session.after_answer("do you fix roofs?", "Yes, we repair roofs.") == (
        "Yes, we repair roofs.", None,
    )


def test_asking_the_same_question_again_transfers() -> None:
    session = CallSession(max_minutes=10)
    session.before_answer("how long does a roof inspection take")
    assert session.before_answer("how long does the roof inspection take") == (
        "transfer", "repeated_question",
    )
    assert not is_repeat("hi", "hi")  # too short to judge


def test_session_state_round_trips_between_turns() -> None:
    session = CallSession(max_minutes=10)
    session.before_answer("do you fix roofs?")
    session.after_answer("do you fix roofs?", "Yes, we repair roofs.")
    session.on_silence()
    restored = CallSession.from_state(session.to_state(), max_minutes=10, started=session.started)
    assert restored.to_state() == session.to_state()
    assert restored.history == session.history


def test_two_silences_in_a_row_end_the_call() -> None:
    session = CallSession(max_minutes=10)
    assert session.on_silence() is False
    session.before_answer("hello?")  # speech resets the count
    assert session.on_silence() is False
    assert session.on_silence() is True


def test_the_time_limit_hands_over_after_answering() -> None:
    session = CallSession(max_minutes=1, started=0.0)
    line, reason = session.after_answer("one more thing", "Sure, we do that.")
    assert reason == "time_limit"
    assert line.startswith("Sure, we do that.")


async def _answer_with(completion_text: str, stop_reason: str = "stop") -> tuple[str | None, AsyncMock]:
    from api.llm.provider import Completion

    generate = AsyncMock(return_value=Completion(
        text=completion_text, model="gpt-oss:20b", input_tokens=1, output_tokens=1,
        stop_reason=stop_reason,
    ))
    provider = SimpleNamespace(generate=generate, aclose=AsyncMock())
    chunk = SimpleNamespace(chunk_id="c1", content="We repair roofs.")
    with (
        patch("api.voice_agent.agent.get_llm_config", AsyncMock(return_value=SimpleNamespace(
            embedding_model="emb", model="gpt-oss:20b",
        ))),
        patch("api.voice_agent.agent.retrieve_hybrid", AsyncMock(return_value=SimpleNamespace(chunks=[chunk]))),
        patch("api.voice_agent.agent.provider_for", return_value=provider),
    ):
        reply = await answer_from_knowledge(object(), object(), "do you fix roofs?", [])  # type: ignore[arg-type]
    return reply, generate


async def test_the_spoken_answer_gets_the_same_token_budget_as_the_chat(env: None) -> None:
    from api.config import get_api_settings

    reply, generate = await _answer_with("Yes, we repair roofs.")

    assert reply == "Yes, we repair roofs."
    # Reasoning models (gpt-oss) spend part of max_tokens thinking -- a small
    # voice-only cap left nothing for the answer, so every question "missed".
    assert generate.await_args.kwargs["max_tokens"] == get_api_settings().llm_max_tokens


async def test_an_empty_or_cut_off_completion_counts_as_a_miss(env: None) -> None:
    reply, _ = await _answer_with("", stop_reason="length")
    assert reply is None


@pytest.mark.parametrize(
    ("when", "open_"),
    [
        (datetime(2026, 10, 5, 14, 0, tzinfo=UTC), True),   # Mon 10:00 EDT
        (datetime(2026, 10, 5, 22, 0, tzinfo=UTC), False),  # Mon 18:00 EDT (closing)
        (datetime(2026, 10, 3, 14, 0, tzinfo=UTC), False),  # Saturday
    ],
)
def test_business_hours(when: datetime, open_: bool) -> None:
    assert in_business_hours(VoiceAgentConfig(), when) is open_


# -- ticket, signature, token -------------------------------------------------------------


def test_call_ticket_round_trips_and_rejects_tampering(env: None) -> None:
    ticket = call_ticket("call1")
    assert verify_call_ticket(ticket) == "call1"
    assert verify_call_ticket(ticket.replace("call1", "call2")) is None
    assert verify_call_ticket(None) is None
    # Only [A-Za-z0-9+-_()] survive as a Browser SDK extra header.
    assert all(c.isalnum() or c in "+-_()" for c in ticket)


def test_plivo_signature_matches_the_documented_layout() -> None:
    # plivo-python's construct_post_url: "?" + re-sorted query + "." then sorted body.
    url = "https://api.example.test/p?ticket=t1&a=1"
    params = {"Speech": "hiThere", "CallUUID": "u1"}
    signed = "https://api.example.test/p?a=1&ticket=t1.CallUUIDu1SpeechhiThere.n1"
    expected = base64.b64encode(hmac.new(b"tok", signed.encode(), hashlib.sha256).digest()).decode()
    assert compute_signature(url, params, "n1", "tok") == expected
    # Several signatures may arrive comma-separated; any match passes.
    verify_signature(url=url, params=params, auth_token="tok", signature=f"other,{expected}", nonce="n1")


async def test_browser_token_comes_from_the_plivo_rest_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"api_id": "x", "token": "jwt-from-plivo"})

    real_client = httpx.AsyncClient
    with patch(
        "api.voice_agent.plivo.httpx.AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    ):
        token = await mint_browser_token(
            auth_id="MAx", auth_token="t", endpoint_username="voiceagent", app_id="42",
        )

    assert token == "jwt-from-plivo"
    [request] = seen
    assert str(request.url) == "https://api.plivo.com/v1/Account/MAx/JWT/Token/"
    body = json.loads(request.content)
    assert body["sub"] == "voiceagent" and body["app"] == "42"
    assert body["per"] == {"voice": {"incoming_allow": False, "outgoing_allow": True}}
    assert body["exp"] - body["nbf"] == 300


# -- Plivo webhooks ---------------------------------------------------------------------


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


async def _post(
    path: str, params: dict[str, str], *, call: VoiceAgentCall | None = None,
    signature: str | None = None, answer: AsyncMock | None = None, open_: bool = True,
) -> tuple[httpx.Response, AsyncMock, AsyncMock, AsyncMock]:
    update, save, record = AsyncMock(), AsyncMock(), AsyncMock()
    with (
        patch("api.voice_agent.routes.get_voice_agent_call", AsyncMock(return_value=call or _call())),
        patch(
            "api.voice_agent.routes.get_voice_agent_config_by_tenant_id",
            AsyncMock(return_value=VoiceAgentConfig(enabled=True)),
        ),
        patch("api.voice_agent.routes.update_voice_agent_call", update),
        patch("api.voice_agent.routes.save_voice_agent_session", save),
        patch("api.voice_agent.routes.append_message", record),
        patch("api.voice_agent.routes.answer_from_knowledge", answer or AsyncMock(return_value=None)),
        patch("api.voice_agent.routes.in_business_hours", return_value=open_),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://api_1:8000") as c:
            response = await c.post(
                path,
                content=urlencode(params),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Plivo-Signature-V3": (
                        signature or compute_signature(_BASE + path, params, "n1", _AUTH)
                    ),
                    "X-Plivo-Signature-V3-Nonce": "n1",
                },
            )
    return response, update, save, record


def _turn(call_id: str = "call1") -> str:
    return f"/public/voice-agent/calls/{call_id}/turn?ticket={call_ticket(call_id)}"


async def test_answer_greets_and_listens_for_speech(env: None) -> None:
    response, update, _, record = await _post(
        "/public/voice-agent/answer", {"CallUUID": "uuid-1", "X-PH-Ticket": call_ticket("call1")},
    )

    assert response.status_code == 200
    assert '<GetInput action="https://api.example.test/public/voice-agent/calls/call1/turn' in response.text
    assert 'inputType="speech"' in response.text
    assert "thanks for calling" in response.text
    assert update.await_args.kwargs["status"] == "in_progress"
    assert update.await_args.kwargs["plivo_call_uuid"] == "uuid-1"
    assert record.await_args.kwargs["role"] == "assistant"


async def test_webhooks_reject_a_bad_signature_or_ticket(env: None) -> None:
    bad_sig, *_ = await _post(
        "/public/voice-agent/answer", {"X-PH-Ticket": call_ticket("call1")}, signature="nope",
    )
    bad_ticket, *_ = await _post("/public/voice-agent/answer", {"X-PH-Ticket": "call1_forged"})
    other_call, *_ = await _post(
        f"/public/voice-agent/calls/call2/turn?ticket={call_ticket('call1')}", {"Speech": "hi"},
    )
    assert bad_sig.status_code == 401
    assert bad_ticket.status_code == 401
    assert other_call.status_code == 401


async def test_a_turn_answers_from_the_knowledge_base_and_keeps_listening(env: None) -> None:
    answer = AsyncMock(return_value="We inspect roofs Monday to Friday.")
    response, _, save, record = await _post(_turn(), {"Speech": "When do you inspect?"}, answer=answer)

    assert "We inspect roofs Monday to Friday." in response.text
    assert "<GetInput" in response.text
    contents = [c.kwargs["content"] for c in record.await_args_list]
    assert contents == ["When do you inspect?", "We inspect roofs Monday to Friday."]
    assert all(c.kwargs["intent"] == "voice_call" for c in record.await_args_list)
    assert save.await_args.args[2]["last_prompt"] == "When do you inspect?"


@pytest.mark.parametrize("open_", [True, False])
async def test_a_question_it_cant_answer_dials_the_saved_number_at_any_hour(
    env: None, open_: bool,
) -> None:
    response, update, _, record = await _post(
        _turn(), {"Speech": "do you do pools?"}, answer=AsyncMock(return_value=None), open_=open_,
    )

    assert "don't have that information" in _unxml(response.text)
    assert "<Number>+17868233553</Number>" in response.text
    assert 'callerId="+15005550006"' in response.text
    assert "dial-status" in response.text
    assert update.await_args.kwargs["status"] == "transferring"
    assert update.await_args.kwargs["transfer_reason"] == "could_not_answer"
    assert any(c.kwargs["content"].startswith("[Call handed to the team") for c in record.await_args_list)


async def test_a_knowledge_base_failure_dials_the_saved_number_after_hours(env: None) -> None:
    broken = AsyncMock(side_effect=RuntimeError("LLM down"))
    response, update, *_ = await _post(_turn(), {"Speech": "do you do pools?"}, answer=broken, open_=False)

    assert "<Number>+17868233553</Number>" in response.text
    assert update.await_args.kwargs["transfer_reason"] == "agent_error"


async def test_a_transfer_outside_business_hours_offers_booking_instead(env: None) -> None:
    response, update, *_ = await _post(_turn(), {"Speech": "Transfer me to a human"}, open_=False)

    assert AFTER_HOURS_LINE in _unxml(response.text)
    assert "<Hangup/>" in response.text
    assert update.await_args.kwargs["status"] == "after_hours"


async def test_silence_reprompts_once_then_hangs_up(env: None) -> None:
    first, *_ = await _post(_turn(), {"Speech": ""})
    assert "didn't catch that" in _unxml(first.text)
    assert "<GetInput" in first.text

    silent_once = CallSession(max_minutes=10)
    silent_once.on_silence()
    second, update, *_ = await _post(_turn(), {"Speech": ""}, call=_call(session=silent_once.to_state()))
    assert GOODBYE_LINE in second.text
    assert "<Hangup/>" in second.text
    assert update.await_args.kwargs["status"] == "completed"


async def test_an_unanswered_transfer_offers_booking(env: None) -> None:
    path = f"/public/voice-agent/calls/call1/dial-status?ticket={call_ticket('call1')}"
    response, update, *_ = await _post(path, {"DialStatus": "no-answer"})

    assert "booking form" in response.text
    assert update.await_args.kwargs["status"] == "transfer_unanswered"


async def test_hangup_mid_call_closes_the_call_row(env: None) -> None:
    response, update, *_ = await _post(
        "/public/voice-agent/hangup",
        {"CallUUID": "uuid-1", "X-PH-Ticket": call_ticket("call1")},
        call=_call(status="in_progress"),
    )

    assert response.status_code == 200
    assert update.await_args.kwargs == {"status": "completed", "ended": True}


# -- visitor start ---------------------------------------------------------------------


def _visitor_app() -> Any:
    from common.auth import AuthClaims, Role

    from api.gateway.dependencies import get_visitor_claims

    app = _app()
    claims = AuthClaims(subject="visitor-1", role=Role.VISITOR, tenant_id="tenant-a")
    app.dependency_overrides[get_visitor_claims] = lambda: claims
    return app


async def _start(mint: AsyncMock, update: AsyncMock | None = None) -> httpx.Response:
    with (
        patch(
            "api.voice_agent.routes.get_voice_agent_config",
            AsyncMock(return_value=VoiceAgentConfig(enabled=True)),
        ),
        patch("api.voice_agent.routes.get_conversation", AsyncMock(return_value=object())),
        patch("api.voice_agent.routes.create_voice_agent_call", AsyncMock(return_value="call1")),
        patch("api.voice_agent.routes.update_voice_agent_call", update or AsyncMock()),
        patch("api.voice_agent.routes.mint_browser_token", mint),
    ):
        async with AsyncClient(transport=ASGITransport(app=_visitor_app()), base_url="http://test") as c:
            return await c.post("/public/voice-agent/calls", json={"conversation_id": "conv-1"})


async def test_start_call_returns_a_token_and_ticket_on_the_visitors_conversation(env: None) -> None:
    mint = AsyncMock(return_value="jwt-from-plivo")
    response = await _start(mint)

    body = response.json()
    assert response.status_code == 200
    assert body["conversation_id"] == "conv-1"
    assert verify_call_ticket(body["ticket"]) == "call1"
    assert body["token"] == "jwt-from-plivo"
    assert mint.await_args.kwargs["endpoint_username"] == "voiceagent"


async def test_start_call_fails_loudly_when_plivo_refuses_the_token(env: None) -> None:
    update = AsyncMock()
    response = await _start(AsyncMock(side_effect=httpx.ConnectError("down")), update)

    assert response.status_code == 500
    assert response.json()["error_code"] == "VOICE_AGENT_TOKEN_FAILED"
    assert update.await_args.kwargs == {"status": "failed", "ended": True}


async def test_start_call_is_refused_when_the_chatbot_has_it_off(env: None) -> None:
    with patch(
        "api.voice_agent.routes.get_voice_agent_config", AsyncMock(return_value=VoiceAgentConfig()),
    ):
        async with AsyncClient(transport=ASGITransport(app=_visitor_app()), base_url="http://test") as c:
            response = await c.post("/public/voice-agent/calls", json={})

    assert response.status_code == 422
    assert response.json()["error_code"] == "VOICE_AGENT_UNAVAILABLE"
