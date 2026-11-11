"""The speed test (2026-11-07): Rebecca over Plivo two-way audio streaming. Plivo's socket,
Deepgram Flux and ElevenLabs are faked; the turn logic is a stub brain."""
from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from api.call_agent import realtime


class _Plivo:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def receive_text(self) -> str:
        return await self.inbox.get()

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def push(self, **event: Any) -> None:
        self.inbox.put_nowait(json.dumps(event))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["event"] == name]


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

    def hear(self, event: str, transcript: str = "") -> None:
        self.queue.put_nowait(realtime.Heard(event, transcript))


class _Voice:
    """Each reply is two audio chunks; ``hold`` keeps the second one back (she's mid-sentence)."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.hold: asyncio.Event | None = None

    async def __call__(self, text: str) -> AsyncIterator[bytes]:
        self.said.append(text)
        yield b"\xff" * 160
        if self.hold is not None:
            await self.hold.wait()
        yield b"\x7f" * 160


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


def _call(brain: Any, said: list[tuple[str, int]] | None = None, **kwargs: Any) -> tuple[
        realtime.RealtimeCall, _Plivo, _Flux, _Voice]:
    plivo, flux, voice = _Plivo(), _Flux(), _Voice()

    async def record(text: str, latency_ms: int) -> None:
        if said is not None:
            said.append((text, latency_ms))

    call = realtime.RealtimeCall(plivo, flux, voice, brain, opening="Hi, Rebecca here.",
                                 record=record, **kwargs)
    return call, plivo, flux, voice


async def test_the_opening_is_spoken_as_soon_as_the_stream_starts() -> None:
    said: list[tuple[str, int]] = []

    async def brain(speech: str) -> tuple[str, bool]:
        raise AssertionError("nobody spoke yet")

    call, plivo, flux, voice = _call(brain, said)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1", "callId": "c-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    assert voice.said == ["Hi, Rebecca here."]
    audio = plivo.events("playAudio")
    assert audio[0]["media"] == {"contentType": "audio/x-mulaw", "sampleRate": 8000,
                                 "payload": base64.b64encode(b"\xff" * 160).decode()}
    assert said and said[0][0] == "Hi, Rebecca here."
    plivo.push(event="media", media={"payload": base64.b64encode(b"\x01\x02").decode()})
    await _until(lambda: flux.audio == [b"\x01\x02"])  # the caller's audio goes to Flux
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_the_end_of_their_turn_gets_her_reply_and_its_timing() -> None:
    said: list[tuple[str, int]] = []
    heard: list[str] = []

    async def brain(speech: str) -> tuple[str, bool]:
        heard.append(speech)
        return "We charge two hundred dollars.", False

    call, plivo, flux, voice = _call(brain, said)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("StartOfTurn", "how much")
    flux.hear("EndOfTurn", "how much is a repair")
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    assert heard == ["how much is a repair"]
    assert voice.said[-1] == "We charge two hundred dollars."
    assert said[-1][0] == "We charge two hundred dollars." and said[-1][1] >= 0
    assert plivo.events("clearAudio") == []  # she wasn't talking when they spoke
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_talking_over_her_stops_her_at_once() -> None:
    async def brain(speech: str) -> tuple[str, bool]:
        return "A long answer that goes on and on.", False

    call, plivo, flux, voice = _call(brain)
    voice.hold = asyncio.Event()  # she's mid-sentence until released
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("playAudio")) == 1)
    flux.hear("StartOfTurn", "sorry, wait")
    await _until(lambda: len(plivo.events("clearAudio")) == 1)
    assert plivo.events("clearAudio")[0] == {"event": "clearAudio", "streamId": "s-1"}
    voice.hold.set()
    await asyncio.sleep(0.02)
    assert len(plivo.events("playAudio")) == 1  # the rest of that sentence is never sent
    assert plivo.events("checkpoint") == []
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_the_last_line_is_heard_before_the_call_ends() -> None:
    async def brain(speech: str) -> tuple[str, bool]:
        return "Thanks for your time, goodbye.", True

    call, plivo, flux, voice = _call(brain)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("EndOfTurn", "bye")
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    assert not plivo.closed  # still saying goodbye
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[1]["name"])
    await asyncio.wait_for(task, 1)
    assert plivo.closed  # stream over -> the call hangs up (nothing after <Stream>)


async def test_silence_after_she_speaks_is_a_turn_too() -> None:
    heard: list[str] = []

    async def brain(speech: str) -> tuple[str, bool]:
        heard.append(speech)
        return "Sorry, I didn't catch that. Are you still there?", False

    call, plivo, flux, voice = _call(brain, silence_seconds=0.05)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    await _until(lambda: heard == [""])
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


def test_only_listed_tenants_with_both_keys_get_the_stream(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(realtime_voice_tenants="t-a, t-c", deepgram_api_key="k1",
                               elevenlabs_api_key="k2", elevenlabs_voice_id="v1",
                               public_api_base_url="https://api.example.com")
    monkeypatch.setattr(realtime, "get_api_settings", lambda: settings)
    assert realtime.enabled_for("t-a") and realtime.enabled_for("t-c")
    assert not realtime.enabled_for("t-b") and not realtime.enabled_for(None)
    settings.elevenlabs_api_key = None
    assert not realtime.enabled_for("t-a")

    xml = realtime.stream_xml("call-1", "tick-1")
    assert 'bidirectional="true"' in xml and 'keepCallAlive="true"' in xml
    assert 'contentType="audio/x-mulaw;rate=8000"' in xml
    assert ("wss://api.example.com/public/call-agent/calls/call-1/stream?ticket=tick-1"
            in xml)
