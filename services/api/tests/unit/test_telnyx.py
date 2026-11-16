"""Telnyx (T1-T3): the dialer over Telnyx's Voice API, its signed webhooks mapped onto our
call events, and the audio-stream adapter that lets the SAME streamed-call logic run on it.
Facts checked against Telnyx's OpenAPI spec and SDK source on 2026-11-16 (specs/calling_agent.md)."""
from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from api.agents.executors import TransientExecutorError
from api.call_agent import realtime, telnyx
from api.call_agent.telephony import CallProviderError, TelnyxTelephony
from api.config import get_api_settings

# -- T2: webhook signature (Ed25519 over "<timestamp>|<raw body>", base64 key + signature) ----


def _keys() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    return private, public.decode()


def _sign(private: Ed25519PrivateKey, body: bytes, timestamp: str) -> str:
    return base64.b64encode(private.sign(timestamp.encode() + b"|" + body)).decode()


def test_a_webhook_signed_by_telnyx_is_accepted() -> None:
    private, public = _keys()
    body = b'{"data":{"event_type":"call.answered"}}'
    telnyx.verify_signature(public_key=public, body=body, timestamp="1000",
                            signature=_sign(private, body, "1000"), now=1000.0)


@pytest.mark.parametrize("case", ["tampered_body", "other_key", "stale", "from_the_future",
                                  "no_signature", "no_timestamp", "garbage_signature",
                                  "garbage_timestamp", "no_public_key"])
def test_every_bad_webhook_is_the_same_rejection(case: str) -> None:
    private, public = _keys()
    other, _ = _keys()
    body = b'{"data":{"event_type":"call.answered"}}'
    kwargs: dict[str, Any] = {"public_key": public, "body": body, "timestamp": "1000",
                              "signature": _sign(private, body, "1000"), "now": 1000.0}
    if case == "tampered_body":
        kwargs["body"] = body + b" "
    elif case == "other_key":
        kwargs["signature"] = _sign(other, body, "1000")
    elif case == "stale":
        kwargs["now"] = 1000.0 + 301
    elif case == "from_the_future":
        kwargs["now"] = 1000.0 - 301
    elif case == "no_signature":
        kwargs["signature"] = None
    elif case == "no_timestamp":
        kwargs["timestamp"] = None
    elif case == "garbage_signature":
        kwargs["signature"] = "not base64 !!"
    elif case == "garbage_timestamp":
        kwargs["timestamp"] = "soon"
    elif case == "no_public_key":
        kwargs["public_key"] = ""
    with pytest.raises(telnyx.TelnyxSignatureInvalidError) as raised:
        telnyx.verify_signature(**kwargs)
    assert raised.value.code == "TELNYX_SIGNATURE_INVALID"


# -- T2: their events -> ours ----------------------------------------------------------------


def _event(event_type: str, **payload: Any) -> bytes:
    return json.dumps({"data": {"record_type": "event", "event_type": event_type, "id": "ev-1",
                                "payload": {"call_control_id": "v2:abc", **payload}}}).encode()


def test_answered_machine_and_human_events() -> None:
    answered = telnyx.parse_event(_event("call.answered", state="answered"))
    assert (answered.kind, answered.call_control_id, answered.event_id) == (
        "answered", "v2:abc", "ev-1")
    assert telnyx.parse_event(_event("call.machine.detection.ended", result="machine")).kind == (
        "machine")
    assert telnyx.parse_event(_event("call.machine.detection.ended", result="human")).kind == (
        "other")
    assert telnyx.parse_event(_event("call.machine.detection.ended", result="not_sure")).kind == (
        "other")
    assert telnyx.parse_event(_event("call.initiated", direction="outgoing")).kind == "other"
    assert telnyx.parse_event(_event("streaming.failed", failure_reason="connection_failed")
                              ).kind == "stream_failed"


@pytest.mark.parametrize(("cause", "status"), [
    ("normal_clearing", "completed"), ("time_limit", "completed"),
    ("timeout", "no-answer"), ("no_answer", "no-answer"),
    ("user_busy", "busy"), ("call_rejected", "busy"), ("originator_cancel", "cancel"),
    ("not_found", "failed"), ("unspecified", "failed"), ("something_new", "failed"),
])
def test_a_hangup_is_told_the_way_the_call_logic_already_reads_it(cause: str, status: str) -> None:
    event = telnyx.parse_event(_event("call.hangup", hangup_cause=cause, hangup_source="callee"))
    assert event.kind == "hangup"
    assert event.params == {"CallStatus": status, "HangupSource": "callee",
                            "HangupCauseName": cause, "CallUUID": "v2:abc"}


def test_a_saved_recording_carries_its_id_when_telnyx_sends_one() -> None:
    saved = telnyx.parse_event(_event("call.recording.saved", recording_id="rec-9",
                                      recording_urls={"mp3": "https://x/rec.mp3"}))
    assert (saved.kind, saved.recording_id) == ("recording_saved", "rec-9")
    assert telnyx.parse_event(_event("call.recording.saved", recording_urls={"mp3": "u"})
                              ).recording_id is None


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"data": 5}',
                                  b'{"data": {"event_type": "call.answered"}}'])
def test_a_malformed_event_is_ignored_not_a_crash(body: bytes) -> None:
    assert telnyx.parse_event(body).kind == "other"


# -- T1: the dialer ---------------------------------------------------------------------------


class _Api:
    """Telnyx's API, recorded: every request, with a canned answer per (method, path)."""

    def __init__(self, answers: dict[tuple[str, str], Any] | None = None, status: int = 200
                 ) -> None:
        self.requests: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        self.answers, self.status = answers or {}, status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.raw_path.decode()
        self.requests.append((request.method, path, body, dict(request.headers)))
        answer = self.answers.get((request.method, path.split("?")[0]), {"data": {"result": "ok"}})
        if isinstance(answer, bytes):
            return httpx.Response(self.status, content=answer)
        return httpx.Response(self.status, json=answer)


def _provider(api: _Api, key: str) -> TelnyxTelephony:
    return TelnyxTelephony(key, "conn-1", transport=httpx.MockTransport(api))


def _fake_key() -> str:
    from uuid import uuid4

    return uuid4().hex


async def test_dialling_asks_for_the_two_way_phone_audio_stream_and_voicemail_detection() -> None:
    api = _Api({("POST", "/v2/calls"): {"data": {"call_control_id": "v2:abc",
                                                 "call_leg_id": "leg-1"}}})
    key = _fake_key()
    placed = await _provider(api, key).dial(
        to_e164="+12025550123", from_e164="+17865550000", time_limit=300,
        webhook_url="https://api.example.com/hook?ticket=t",
        stream_url="wss://api.example.com/stream?ticket=t")
    assert (placed.request_uuid, placed.call_uuid) == ("leg-1", "v2:abc")
    [(method, path, body, headers)] = api.requests
    assert (method, path) == ("POST", "/v2/calls")
    assert headers["authorization"] == f"Bearer {key}"
    assert body == {
        "connection_id": "conn-1", "to": "+12025550123", "from": "+17865550000",
        "webhook_url": "https://api.example.com/hook?ticket=t",
        "timeout_secs": 30, "time_limit_secs": 300, "answering_machine_detection": "detect",
        "stream_url": "wss://api.example.com/stream?ticket=t", "stream_track": "inbound_track",
        "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU"}


@pytest.mark.parametrize(("status", "error"), [(401, CallProviderError), (422, CallProviderError),
                                               (429, TransientExecutorError),
                                               (503, TransientExecutorError)])
async def test_a_refusal_is_final_and_an_outage_is_retried(status: int, error: type[Exception]
                                                           ) -> None:
    with pytest.raises(error):
        await _provider(_Api(status=status), _fake_key()).hangup(call_uuid="v2:abc")


async def test_an_unreachable_api_is_retried() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    provider = TelnyxTelephony(_fake_key(), "conn-1", transport=httpx.MockTransport(down))
    with pytest.raises(TransientExecutorError):
        await provider.hangup(call_uuid="v2:abc")


async def test_a_dial_without_a_call_id_is_a_refusal() -> None:
    api = _Api({("POST", "/v2/calls"): {"data": {}}})
    with pytest.raises(CallProviderError):
        await _provider(api, _fake_key()).dial(
            to_e164="+12025550123", from_e164="+17865550000", time_limit=300,
            webhook_url="https://h", stream_url="wss://s")


async def test_recording_starts_stops_and_the_call_can_be_hung_up_or_transferred() -> None:
    api = _Api()
    provider = _provider(api, _fake_key())
    started = await provider.start_recording(call_uuid="v2:abc", time_limit=300)
    # Telnyx names the recording only once it is saved: until then it is "this call's".
    assert (started.recording_id, started.url) == ("pending:v2:abc", None)
    await provider.stop_recording(call_uuid="v2:abc")
    await provider.hangup(call_uuid="v2:abc")
    await provider.transfer(call_uuid="v2:abc", to_e164="+13055550111", from_e164="+17865550000")
    assert [(m, p, b) for m, p, b, _ in api.requests] == [
        ("POST", "/v2/calls/v2:abc/actions/record_start",
         {"format": "mp3", "channels": "single", "max_length": 300}),
        ("POST", "/v2/calls/v2:abc/actions/record_stop", {}),
        ("POST", "/v2/calls/v2:abc/actions/hangup", {}),
        ("POST", "/v2/calls/v2:abc/actions/transfer",
         {"to": "+13055550111", "from": "+17865550000"})]


async def test_deleting_by_id_or_every_recording_of_a_call() -> None:
    api = _Api({("GET", "/v2/recordings"): {"data": [{"id": "rec-1"}, {"id": "rec-2"}]}})
    provider = _provider(api, _fake_key())
    await provider.delete_recording(recording_id="rec-9")
    await provider.delete_recording(recording_id="pending:v2:abc")
    calls = [(m, p) for m, p, _, _ in api.requests]
    assert calls[0] == ("DELETE", "/v2/recordings/rec-9")
    assert calls[1][0] == "GET" and calls[1][1].startswith("/v2/recordings?")
    assert "v2%3Aabc" in calls[1][1] and "call_control_id" in calls[1][1]
    assert calls[2:] == [("DELETE", "/v2/recordings/rec-1"), ("DELETE", "/v2/recordings/rec-2")]
    assert await provider.recording_ids(call_uuid="v2:abc") == ["rec-1", "rec-2"]


async def test_a_recording_is_fetched_through_a_fresh_download_link() -> None:
    """Telnyx's download links expire: we store the recording's id and ask for a link each
    time -- and only fetch a link Telnyx's own API handed us."""
    api = _Api({("GET", "/v2/recordings/rec-9"): {"data": {"download_urls": {
        "mp3": "https://files.example.com/rec-9.mp3?sig=1"}}},
        ("GET", "/rec-9.mp3"): b"ID3-audio"})
    provider = _provider(api, _fake_key())
    assert await provider.fetch_recording(url="telnyx:rec-9") == b"ID3-audio"
    download = api.requests[-1]
    assert download[1].startswith("/rec-9.mp3") and "authorization" not in download[3]
    with pytest.raises(CallProviderError):
        await provider.fetch_recording(url="https://elsewhere.example.com/x.mp3")


async def test_the_old_xml_transfer_does_not_exist_on_telnyx() -> None:
    with pytest.raises(CallProviderError):
        await _provider(_Api(), _fake_key()).transfer_to_url(call_uuid="v2:abc", url="https://x")


# -- T3: the audio-stream adapter -------------------------------------------------------------


class _Ws:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed: int | None = None

    async def receive_text(self) -> str:
        return await self.inbox.get()

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    def push(self, **event: Any) -> None:
        self.inbox.put_nowait(json.dumps(event))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_their_stream_messages_become_the_ones_the_call_logic_knows() -> None:
    ws = _Ws()
    socket = telnyx.TelnyxSocket(ws)
    ws.push(event="connected", version="1.0.0")  # nothing to do: skipped
    ws.push(event="start", stream_id="s-1", start={"call_control_id": "v2:abc"})
    ws.push(event="media", stream_id="s-1", media={"track": "outbound", "payload": _b64(b"me")})
    ws.push(event="media", stream_id="s-1", media={"track": "inbound", "payload": _b64(b"hi")})
    ws.push(event="error", payload={"code": 100003, "title": "malformed_frame"})  # logged, skipped
    ws.push(event="dtmf", dtmf={"digit": "1"})
    ws.push(event="mark", stream_id="s-1", mark={"name": "reply-1"})
    ws.push(event="stop", stream_id="s-1", stop={"call_control_id": "v2:abc"})
    got = [json.loads(await socket.receive_text()) for _ in range(4)]
    assert got == [
        {"event": "start", "start": {"streamId": "s-1", "callId": "v2:abc"}},
        {"event": "media", "media": {"payload": _b64(b"hi")}},  # the caller only, never her own
        {"event": "playedStream", "name": "reply-1"},
        {"event": "stop"}]


async def test_her_audio_goes_out_in_whole_20ms_frames_then_the_mark() -> None:
    """Telnyx takes audio in chunks of 20 ms or more; the voice hands us any size."""
    ws = _Ws()
    socket = telnyx.TelnyxSocket(ws)
    play = {"event": "playAudio", "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000}}
    await socket.send_text(json.dumps({**play, "media": {"payload": _b64(b"\x01" * 100)}}))
    assert ws.sent == []  # under one frame: held back
    await socket.send_text(json.dumps({**play, "media": {"payload": _b64(b"\x02" * 300)}}))
    [first] = ws.sent
    assert first["event"] == "media"
    assert base64.b64decode(first["media"]["payload"]) == b"\x01" * 100 + b"\x02" * 220
    await socket.send_text(json.dumps({"event": "checkpoint", "streamId": "s-1",
                                       "name": "reply-1"}))
    tail, mark = ws.sent[1:]
    assert base64.b64decode(tail["media"]["payload"]) == b"\x02" * 80 + b"\xff" * 80  # padded
    assert mark == {"event": "mark", "mark": {"name": "reply-1"}}


async def test_talking_over_her_clears_what_is_queued_and_what_is_held() -> None:
    ws = _Ws()
    socket = telnyx.TelnyxSocket(ws)
    await socket.send_text(json.dumps({"event": "playAudio",
                                       "media": {"payload": _b64(b"\x01" * 100)}}))
    await socket.send_text(json.dumps({"event": "clearAudio", "streamId": "s-1"}))
    await socket.send_text(json.dumps({"event": "checkpoint", "streamId": "s-1", "name": "r"}))
    assert ws.sent == [{"event": "clear"}, {"event": "mark", "mark": {"name": "r"}}]
    await socket.close(1011)
    assert ws.closed == 1011


class _Flux:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[realtime.Heard | None] = asyncio.Queue()
        self.audio: list[bytes] = []

    async def send(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def events(self) -> AsyncIterator[realtime.Heard]:
        while (heard := await self.queue.get()) is not None:
            yield heard

    async def close(self) -> None:
        self.queue.put_nowait(None)


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


async def test_a_whole_streamed_call_runs_on_telnyx_unchanged() -> None:
    """The same RealtimeCall as on Plivo: opening, the caller's turn, her reply, goodbye."""
    ws, flux = _Ws(), _Flux()

    async def voice(text: str) -> AsyncIterator[bytes]:
        yield b"\x7f" * 400

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        return "Thanks for your time, goodbye.", True

    async def record(text: str, latency_ms: int) -> None:
        return None

    call = realtime.RealtimeCall(telnyx.TelnyxSocket(ws), flux, voice, brain,
                                 opening="Hi, Rebecca here.", record=record)
    task = asyncio.create_task(call.run())
    ws.push(event="connected", version="1.0.0")
    ws.push(event="start", stream_id="s-1", start={"call_control_id": "v2:abc"})
    await _until(lambda: any(m["event"] == "mark" for m in ws.sent))
    opening_audio = b"".join(base64.b64decode(m["media"]["payload"])
                             for m in ws.sent if m["event"] == "media")
    assert opening_audio == b"\x7f" * 400 + b"\xff" * 80  # whole frames, padded once
    ws.push(event="media", media={"track": "inbound", "payload": _b64(b"\x01\x02")})
    await _until(lambda: flux.audio == [b"\x01\x02"])  # the caller's audio reaches the listener
    ws.push(event="mark", mark={"name": ws.sent[-1]["mark"]["name"]})
    flux.queue.put_nowait(realtime.Heard("EndOfTurn", "bye"))
    await _until(lambda: sum(m["event"] == "mark" for m in ws.sent) == 2)
    assert ws.closed is None  # still saying goodbye
    ws.push(event="mark", mark={"name": ws.sent[-1]["mark"]["name"]})
    await asyncio.wait_for(task, 1)
    assert ws.closed is not None  # her goodbye was heard: the stream ends


# -- who gets Telnyx, and the URLs -----------------------------------------------------------


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key in ("TELNYX_TENANTS", "TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "TELNYX_CONNECTION_ID",
                "TELNYX_FROM_NUMBER", "REALTIME_VOICE_TENANTS", "DEEPGRAM_API_KEY",
                "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        monkeypatch.delenv(key, raising=False)
    base = {"DEPLOYMENT_MODE": "saas", "DATABASE_URL": "postgres://stub-host:5432/appdb",
            "JWT_SECRET": "x" * 48, "SERVICE_NAME": "api",
            "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "PUBLIC_API_BASE_URL": "https://api.example.com"}
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    from common.settings import get_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def test_only_listed_tenants_with_every_key_and_the_stream_get_telnyx(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _, public = _keys()
    full = {"TELNYX_TENANTS": "t-1, t-2", "TELNYX_API_KEY": _fake_key(),
            "TELNYX_PUBLIC_KEY": public, "TELNYX_CONNECTION_ID": "conn-1",
            "TELNYX_FROM_NUMBER": "+17865550000", "REALTIME_VOICE_TENANTS": "t-1,t-2",
            "DEEPGRAM_API_KEY": _fake_key(), "ELEVENLABS_API_KEY": _fake_key(),
            "ELEVENLABS_VOICE_ID": "voice-1"}
    _settings(monkeypatch, **full)
    assert telnyx.enabled_for("t-1") and telnyx.enabled_for("t-2")
    assert not telnyx.enabled_for("t-3") and not telnyx.enabled_for(None)
    for missing in ("TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "TELNYX_CONNECTION_ID",
                    "TELNYX_FROM_NUMBER", "DEEPGRAM_API_KEY"):  # Telnyx calls are streamed calls
        _settings(monkeypatch, **{k: v for k, v in full.items() if k != missing})
        assert not telnyx.enabled_for("t-1"), missing
    get_api_settings.cache_clear()


def test_the_stream_and_event_urls_carry_the_calls_ticket(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    assert telnyx.stream_url("c-1", "tick") == (
        "wss://api.example.com/public/call-agent/calls/c-1/stream?ticket=tick")
    assert telnyx.events_url("c-1", "tick") == (
        "https://api.example.com/public/call-agent/telnyx/calls/c-1/events?ticket=tick")
    get_api_settings.cache_clear()
