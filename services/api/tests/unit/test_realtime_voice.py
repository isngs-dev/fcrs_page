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

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
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

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
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
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
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


async def test_a_pause_mid_sentence_never_cuts_her_thinking_short() -> None:
    """Live 2026-11-12: "go with fifteen November." [pause] "three thirty PM EST." -- the
    second half cancelled her half-done turn. Now both halves run in order (the turn logic
    keeps its state), and only the reply to the latest words is spoken."""
    heard: list[str] = []
    finished: list[str] = []  # turns whose logic ran to the end (state saved)
    first_done = asyncio.Event()

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        if speech == "go with fifteen November.":
            await first_done.wait()  # still thinking when the rest arrives
            finished.append(speech)
            return "What time on Sunday, November 15?", False
        finished.append(speech)
        return "Just to confirm, Sunday, November 15 at 3:30 PM Eastern time?", False

    call, plivo, flux, voice = _call(brain)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("EndOfTurn", "go with fifteen November.")
    await _until(lambda: heard == ["go with fifteen November."])
    flux.hear("StartOfTurn", "three")
    flux.hear("EndOfTurn", "three thirty PM EST.")
    await asyncio.sleep(0.02)
    first_done.set()
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    assert finished == ["go with fifteen November.", "three thirty PM EST."]
    assert voice.said == ["Hi, Rebecca here.",
                          "Just to confirm, Sunday, November 15 at 3:30 PM Eastern time?"]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_the_last_line_is_heard_before_the_call_ends() -> None:
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
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


async def test_words_during_her_closing_line_neither_cut_it_off_nor_get_a_turn() -> None:
    """Live 2026-11-14: "Okay." during "You're booked..." cut the line off and ran the booking
    again -- "we already have a meeting scheduled at that time"."""
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        return "You're booked for Wednesday at 3:30 PM. Goodbye.", True

    call, plivo, flux, voice = _call(brain)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    voice.hold = asyncio.Event()  # the closing line is still playing
    flux.hear("EndOfTurn", "Yes.")
    await _until(lambda: len(plivo.events("playAudio")) == 2)
    flux.hear("StartOfTurn", "okay")
    flux.hear("EndOfTurn", "Okay.")
    await asyncio.sleep(0.02)
    assert plivo.events("clearAudio") == []  # the closing line isn't cut off
    voice.hold.set()
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[1]["name"])
    await asyncio.wait_for(task, 1)
    assert heard == ["Yes."]  # "Okay." never reached the turn logic
    assert plivo.closed


async def test_silence_after_she_speaks_is_a_turn_too() -> None:
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
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


async def test_a_voice_that_fails_ends_the_call_instead_of_dead_air() -> None:
    """Live 2026-11-12: ElevenLabs refused the voice (402, free plan + library voice)."""
    import httpx

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        raise AssertionError("never reached")

    async def refused(text: str) -> AsyncIterator[bytes]:
        request = httpx.Request("POST", "https://api.elevenlabs.io/v1/text-to-speech/v/stream")
        raise httpx.HTTPStatusError("402", request=request,
                                    response=httpx.Response(402, request=request))
        yield b""  # pragma: no cover -- makes this an async generator

    plivo, flux = _Plivo(), _Flux()

    async def record(text: str, latency_ms: int) -> None:
        return None

    call = realtime.RealtimeCall(plivo, flux, refused, brain, opening="Hi.", record=record)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await asyncio.wait_for(task, 1)
    assert plivo.closed and plivo.events("playAudio") == []


def test_flux_writes_numbers_as_digits_and_listens_for_the_business_name() -> None:
    from urllib.parse import parse_qs

    query = parse_qs(realtime.flux_query(["iSN Global Solutions", ""]))
    assert query["model"] == ["flux-general-en"]
    assert (query["encoding"], query["sample_rate"]) == (["mulaw"], ["8000"])
    assert query["numerals"] == ["true"]  # "fifteenth" -> "15th": the date reader needs digits
    assert query["keyterm"] == ["iSN Global Solutions"]


async def test_she_starts_speaking_at_the_first_sentence_and_never_repeats_it() -> None:
    said: list[tuple[str, int]] = []
    rest = asyncio.Event()

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        await speak.sentence("We build Google and Meta ads for roofers.")
        await rest.wait()  # the model is still writing
        return ("We build Google and Meta ads for roofers. And landing pages that book "
                "inspections. Would you like me to find a time?"), False

    call, plivo, flux, voice = _call(brain, said)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("EndOfTurn", "what do you do for roofers")
    await _until(lambda: voice.said[-1] == "We build Google and Meta ads for roofers.")
    assert len(plivo.events("checkpoint")) == 1  # mid-answer: no end-of-reply mark yet
    rest.set()
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    assert voice.said == ["Hi, Rebecca here.", "We build Google and Meta ads for roofers.",
                          "And landing pages that book inspections. Would you like me to find "
                          "a time?"]
    assert said[-1][0].startswith("We build Google and Meta ads for roofers. And landing")
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_when_the_answer_turns_into_a_miss_she_says_the_miss_line_after() -> None:
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        await speak.sentence("Yes, we work with roofers.")
        return "That's a good question for our team. They can go over it with you.", False

    call, plivo, flux, voice = _call(brain)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("EndOfTurn", "do you guarantee results")
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    assert voice.said[1:] == ["Yes, we work with roofers.",
                              "That's a good question for our team. They can go over it with you."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_once_they_talk_over_her_nothing_more_of_that_answer_is_said() -> None:
    more = asyncio.Event()

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        await speak.sentence("First sentence.")
        await more.wait()
        await speak.sentence("Second sentence.")
        return "First sentence. Second sentence. Third sentence.", False

    call, plivo, flux, voice = _call(brain)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("EndOfTurn", "tell me more")
    await _until(lambda: voice.said[-1] == "First sentence.")
    flux.hear("StartOfTurn", "wait")
    await _until(lambda: len(plivo.events("clearAudio")) == 1)
    more.set()
    await asyncio.sleep(0.05)
    assert voice.said == ["Hi, Rebecca here.", "First sentence."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


def test_the_words_flux_listens_for_are_the_business_name_plus_the_configured_ones(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-11-12: "roofs" was heard as "rupees" -- the trade's own words help."""
    from types import SimpleNamespace

    settings = SimpleNamespace(deepgram_keyterms=" roofing, roofers ,Roof Engine,, ")
    monkeypatch.setattr(realtime, "get_api_settings", lambda: settings)
    assert realtime.keyterms("iSN Global Solutions") == [
        "iSN Global Solutions", "roofing", "roofers", "Roof Engine"]
    settings.deepgram_keyterms = None
    assert realtime.keyterms(None) == []


async def test_a_short_bridge_fills_the_wait_and_is_never_part_of_the_answer() -> None:
    """Option A (2026-11-13): "Right." the moment an AI answer starts, instead of silence;
    rotated, never twice in a row, not in the transcript, not repeated by the answer."""
    said: list[tuple[str, int]] = []
    answer_ready = asyncio.Event()

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        await speak.bridge()
        await answer_ready.wait()
        await speak.sentence("We run Google and Meta ads.")
        return "We run Google and Meta ads. And landing pages.", False

    call, plivo, flux, voice = _call(brain, said)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    for n, question in enumerate(("what do you do", "and what else"), start=2):
        plivo.push(event="playedStream", name=plivo.events("checkpoint")[-1]["name"])
        answer_ready.clear()
        flux.hear("EndOfTurn", question)
        await _until(lambda: voice.said[-1] in realtime.BRIDGES)  # said while it thinks
        answer_ready.set()
        await _until(lambda n=n: len(plivo.events("checkpoint")) == n)
        assert voice.said[-2:] == ["We run Google and Meta ads.", "And landing pages."]
        assert said[-1][0] == "We run Google and Meta ads. And landing pages."  # no bridge
    bridges = [line for line in voice.said if line in realtime.BRIDGES]
    assert len(bridges) == 2 and bridges[0] != bridges[1]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_no_bridge_once_she_is_already_answering_or_was_talked_over() -> None:
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        await speak.sentence("Yes.")
        await speak.bridge()  # too late: she's already answering
        return "Yes.", False

    call, plivo, flux, voice = _call(brain)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("EndOfTurn", "do you work with roofers")
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    assert voice.said == ["Hi, Rebecca here.", "Yes."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


# -- Claude for calls, chatbot unchanged (2026-11-13) -----------------------------------------


async def test_calls_use_claude_from_the_server_setting_and_keep_the_tenants_embeddings(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from common.auth import AuthClaims, Role

    from api.llm.config_repository import LLMConfig

    tenant_config = LLMConfig(provider="openai", model="gpt-6-luna", api_key=uuid4().hex,
                              embedding_model="text-embedding-3-small",
                              embedding_base_url="https://api.openai.com/v1",
                              embedding_api_key=uuid4().hex, embedding_dimensions=1536)

    async def tenant_llm(db: object, claims: object) -> LLMConfig:
        return tenant_config

    monkeypatch.setattr(realtime, "get_llm_config", tenant_llm)
    claude_key = uuid4().hex
    settings = SimpleNamespace(realtime_voice_tenants="t-a", deepgram_api_key="k",
                               elevenlabs_api_key="k", elevenlabs_voice_id="v",
                               anthropic_api_key=claude_key, calls_llm_model="claude-haiku-5-5")
    monkeypatch.setattr(realtime, "get_api_settings", lambda: settings)
    claims_a = AuthClaims(subject="s", role=Role.CLIENT_ADMIN, tenant_id="t-a")

    calls = await realtime.calls_llm(object(), claims_a)  # type: ignore[arg-type]
    assert calls is not None
    assert (calls.provider, calls.model, calls.api_key) == ("anthropic", "claude-haiku-5-5",
                                                            claude_key)
    assert calls.base_url is None
    assert (calls.embedding_model, calls.embedding_base_url, calls.embedding_api_key) == (
        tenant_config.embedding_model, tenant_config.embedding_base_url,
        tenant_config.embedding_api_key)  # the knowledge base was embedded there

    claims_b = AuthClaims(subject="s", role=Role.CLIENT_ADMIN, tenant_id="t-b")
    assert await realtime.calls_llm(object(), claims_b) is None  # not on the streamed calls
    settings.calls_llm_model = None
    assert await realtime.calls_llm(object(), claims_a) is None  # no model chosen: unchanged
