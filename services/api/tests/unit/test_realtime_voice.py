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

    kwargs.setdefault("opening_wait", 0)  # most tests: nobody says hello, she opens at once
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
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)  # the opening (never cut off)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    voice.hold = asyncio.Event()  # her reply is mid-sentence until released
    flux.hear("EndOfTurn", "tell me about repairs")
    await _until(lambda: len(plivo.events("playAudio")) == 3)
    flux.hear("StartOfTurn", "sorry, wait")
    await _until(lambda: len(plivo.events("clearAudio")) == 1)
    assert plivo.events("clearAudio")[0] == {"event": "clearAudio", "streamId": "s-1"}
    voice.hold.set()
    await asyncio.sleep(0.02)
    assert len(plivo.events("playAudio")) == 3  # the rest of that sentence is never sent
    assert len(plivo.events("checkpoint")) == 1
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

    call = realtime.RealtimeCall(plivo, flux, refused, brain, opening="Hi.", record=record,
                                 opening_wait=0)
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
    # 2026-11-19: an early "they seem done" too (never above the end-of-turn threshold, 0.7)
    assert 0.3 <= float(query["eager_eot_threshold"][0]) <= 0.7


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

    call, plivo, flux, voice = _call(brain, said, bridge_delay=0.01)
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



async def test_a_turn_with_nothing_to_say_keeps_the_silence_clock_running() -> None:
    """2026-11-16: on hold she says nothing to a silent turn -- the next silent turn must
    still come (so she can check in later), not wait forever."""
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        return "", False  # quiet: the caller asked her to hold

    call, plivo, flux, voice = _call(brain, silence_seconds=0.05)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    opening = len(plivo.events("playAudio"))
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    await _until(lambda: heard == ["", "", ""])
    assert len(plivo.events("playAudio")) == opening  # nothing spoken after the opening
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)



def test_no_bridge_word_sounds_like_a_yes() -> None:
    """Owner, 2026-11-16: "Right." / "Okay." before "No, we don't do that" sounded wrong."""
    agreeing = {"right", "okay", "ok", "sure", "yes", "yeah", "mm-hm", "great", "absolutely"}
    for bridge in realtime.BRIDGES:
        assert not set(bridge.lower().strip(".!").split()) & agreeing, bridge


async def test_a_fast_answer_starts_without_a_bridge() -> None:
    """A bridge only covers a slow answer: one that starts in time is said straight away."""
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        await speak.bridge()
        await speak.sentence("No, we don't do commercial roofs.")  # well within the delay
        return "No, we don't do commercial roofs.", False

    call, plivo, flux, voice = _call(brain, bridge_delay=0.5)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("EndOfTurn", "do you do commercial roofs")
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    await asyncio.sleep(0.6)  # past the delay: the bridge must never come late
    assert voice.said == ["Hi, Rebecca here.", "No, we don't do commercial roofs."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_she_starts_looking_up_the_answer_as_soon_as_they_seem_done() -> None:
    """2026-11-19 (owner: answers must be instant): Flux's EagerEndOfTurn starts the
    knowledge lookup while Flux makes sure; only EndOfTurn is a turn. Its words are always
    the EndOfTurn's own, unless they carry on (TurnResumed), which starts a new lookup."""
    early: list[str] = []
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        return "Yes, we do both.", False

    call, plivo, flux, voice = _call(brain, prefetch=early.append)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    flux.hear("StartOfTurn", "do you")
    flux.hear("EagerEndOfTurn", "do you fix roofs")
    await _until(lambda: early == ["do you fix roofs"])
    assert heard == []  # not a turn yet: they may carry on
    flux.hear("TurnResumed", "do you fix roofs")
    flux.hear("EagerEndOfTurn", "do you fix roofs and gutters")
    flux.hear("EndOfTurn", "do you fix roofs and gutters")
    await _until(lambda: heard == ["do you fix roofs and gutters"])
    assert early == ["do you fix roofs", "do you fix roofs and gutters"]
    assert plivo.events("clearAudio") == []  # carrying on is not talking over her
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


# -- the opening waits for them (live 2026-11-19) -------------------------------------------
# The line counted as answered before the person could hear (CallHippo's app), and a "Hello?"
# over the opening cleared it: they heard only "...is now a good time?". Now she waits for
# their hello -- or a short silence -- and then says the whole opening.


def test_the_wait_for_a_hello_is_short() -> None:
    assert 1.5 <= realtime.OPENING_WAIT <= 3.5


async def test_she_waits_for_their_hello_then_says_the_whole_opening() -> None:
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        raise AssertionError("their hello is not a turn")

    call, plivo, flux, voice = _call(brain, opening_wait=5)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await asyncio.sleep(0.05)
    assert voice.said == [] and plivo.events("playAudio") == []  # nobody there yet
    flux.hear("StartOfTurn", "hello")
    flux.hear("EagerEndOfTurn", "Hello?")
    flux.hear("EndOfTurn", "Hello?")
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    assert voice.said == ["Hi, Rebecca here."]
    assert plivo.events("clearAudio") == []  # their hello didn't cut anything off
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_if_they_say_nothing_she_opens_after_a_short_wait() -> None:
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        raise AssertionError("nobody spoke")

    call, plivo, flux, voice = _call(brain, opening_wait=0.05)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    assert voice.said == ["Hi, Rebecca here."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_while_they_are_still_talking_the_short_wait_doesnt_cut_in() -> None:
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        raise AssertionError("their hello is not a turn")

    call, plivo, flux, voice = _call(brain, opening_wait=0.05)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    flux.hear("StartOfTurn", "hello")
    await asyncio.sleep(0.15)
    assert voice.said == []  # they're mid-hello: she waits for them to finish
    flux.hear("EndOfTurn", "hello who is this")
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    assert voice.said == ["Hi, Rebecca here."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_a_voicemail_greeting_before_she_speaks_goes_to_the_turn_logic() -> None:
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        return "Hi, this is Rebecca, please call us back.", True

    call, plivo, flux, voice = _call(brain, opening_wait=5)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    greeting = "You've reached John, please leave a message after the tone"
    flux.hear("StartOfTurn", "you've")
    flux.hear("EndOfTurn", greeting)
    await _until(lambda: heard == [greeting])
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    assert voice.said == ["Hi, this is Rebecca, please call us back."]  # no opening first
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    await asyncio.wait_for(task, 1)


def test_flux_waits_for_more_certainty_before_it_ends_their_turn() -> None:
    """Live 2026-11-20: a pause after "So..." or "I was saying that..." ended their turn and she
    answered half a sentence. A higher end-of-turn threshold waits for the whole sentence; the
    early ("eager") one still starts the knowledge lookup and must stay below it."""
    from urllib.parse import parse_qs

    params = parse_qs(realtime.flux_query([]))
    # Live 2026-11-20 (Indian lines): 0.8 was rarely reached -- every turn waited out the 3 s
    # timeout and replies came late. Back to Flux's 0.7; cut-offs are joined in code instead.
    eot, eager = float(params["eot_threshold"][0]), float(params["eager_eot_threshold"][0])
    assert eot == 0.7 and eager < eot


# -- Live 2026-11-20: 12-15 s of silence at the start; her opening cut off by "Hello?" ------


def test_the_opening_never_waits_long() -> None:
    assert realtime.OPENING_WAIT < realtime.OPENING_CAP <= 5


async def test_the_opening_starts_within_the_cap_even_if_the_line_never_goes_quiet() -> None:
    """Every StartOfTurn (a "hello?", line noise) used to restart the wait: 12-15 s live."""
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        raise AssertionError("nothing was a turn")

    call, plivo, flux, voice = _call(brain, opening_wait=5, opening_cap=0.15)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    for _ in range(5):
        flux.hear("StartOfTurn", "hello")
        await asyncio.sleep(0.04)
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    assert voice.said == ["Hi, Rebecca here."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_hello_during_the_opening_never_cuts_it_off() -> None:
    """The opening carries the AI disclosure and recording notice: it is always heard whole."""
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        raise AssertionError("a hello over the opening is not a turn")

    call, plivo, flux, voice = _call(brain)
    voice.hold = asyncio.Event()  # the opening is mid-sentence
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("playAudio")) == 1)
    flux.hear("StartOfTurn", "hello")
    flux.hear("EndOfTurn", "Hello?")
    await asyncio.sleep(0.05)
    assert plivo.events("clearAudio") == []
    voice.hold.set()
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    assert len(plivo.events("playAudio")) == 2  # the whole opening went out
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_a_question_during_the_opening_is_answered_right_after_it() -> None:
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        return "We charge two hundred dollars.", False

    call, plivo, flux, voice = _call(brain)
    voice.hold = asyncio.Event()
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("playAudio")) == 1)
    flux.hear("StartOfTurn", "how much")
    flux.hear("EndOfTurn", "how much is a repair")
    await asyncio.sleep(0.05)
    assert voice.said == ["Hi, Rebecca here."]  # her reply waits for the opening
    voice.hold.set()
    await _until(lambda: len(plivo.events("checkpoint")) == 2)
    assert heard == ["how much is a repair"]
    assert voice.said == ["Hi, Rebecca here.", "We charge two hundred dollars."]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


# -- Live 2026-11-20: "So" and "I was saying that" were answered as whole turns -------------


async def _opened(brain: Any, **kwargs: Any) -> tuple[Any, _Plivo, _Flux, _Voice, Any]:
    call, plivo, flux, voice = _call(brain, **kwargs)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    plivo.push(event="playedStream", name=plivo.events("checkpoint")[0]["name"])
    return call, plivo, flux, voice, task


async def test_a_cut_off_turn_waits_for_the_rest_and_is_answered_whole() -> None:
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        return "Yes, they are real homeowners.", False

    _, plivo, flux, _, task = await _opened(brain, fragment_wait=0.3)
    flux.hear("StartOfTurn", "so")
    flux.hear("EndOfTurn", "So")
    await asyncio.sleep(0.1)
    assert heard == []  # held: they're probably not done
    flux.hear("StartOfTurn", "will")
    flux.hear("EndOfTurn", "will the generated leads be genuine?")
    await _until(lambda: heard != [])
    assert heard == ["So will the generated leads be genuine?"]
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


async def test_a_cut_off_turn_with_nothing_after_it_is_still_answered() -> None:
    heard: list[str] = []

    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        heard.append(speech)
        return "Sorry, could you say it again?", False

    _, plivo, flux, _, task = await _opened(brain, fragment_wait=0.1)
    flux.hear("EndOfTurn", "I was saying that")
    await _until(lambda: heard == ["I was saying that"])
    plivo.push(event="stop")
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(("text", "cut_off"), [
    ("So", True), ("I was saying that", True), ("Um,", True), ("and the", True),
    ("Yes.", False), ("No thanks.", False), ("How much is a repair?", False),
    ("I want to know about Google Ads.", False)])
def test_what_counts_as_cut_off(text: str, cut_off: bool) -> None:
    assert realtime.is_cut_off(text) is cut_off


class _BrokenFlux(_Flux):
    async def events(self) -> AsyncIterator[realtime.Heard]:
        while (heard := await self.queue.get()) is not None:
            if heard.event == "boom":
                raise ConnectionError("flux went away")
            yield heard


@pytest.mark.parametrize("how", ["ended", "raised"])
async def test_if_she_can_no_longer_hear_them_the_call_ends_instead_of_going_on_deaf(
        how: str) -> None:
    """Live 2026-11-28 (Telnyx, HomeOwners): mid-call she stopped hearing the caller, then
    said "I didn't catch that" twice and hung up. A listener that's gone never goes unnoticed."""
    async def brain(speech: str, speak: realtime.Speaking) -> tuple[str, bool]:
        return "Sure.", False

    plivo, flux, voice = _Plivo(), _BrokenFlux(), _Voice()

    async def record(text: str, latency_ms: int) -> None:
        return None

    call = realtime.RealtimeCall(plivo, flux, voice, brain, opening="Hi.", record=record,
                                 opening_wait=0, silence_seconds=30)
    task = asyncio.create_task(call.run())
    plivo.push(event="start", start={"streamId": "s-1"})
    await _until(lambda: len(plivo.events("checkpoint")) == 1)
    if how == "ended":
        flux.queue.put_nowait(None)
    else:
        flux.hear("boom")
    await asyncio.wait_for(task, 1)
    assert plivo.closed


# -- Live 2026-11-28 (Roof-Engine, Indian numbers): the booked date and time came out as
# gibberish. Flash v2.5 guesses the language from the text (an Indian name in the sentence) and
# misreads "9:23 AM"-style times (ElevenLabs' normalization guide); her lines go out in English,
# dates and times already in words.


@pytest.mark.parametrize(("written", "spoken"), [
    ("Monday, November 30 at 2 PM Eastern time.",
     "Monday, November thirtieth at two p.m. Eastern time."),
    ("Wednesday, November 25 at 1:30 PM Eastern.",
     "Wednesday, November twenty-fifth at one thirty p.m. Eastern."),
    ("at 9:05 AM", "at nine oh five a.m."),
    ("at 12:00 PM", "at twelve p.m."),
    ("on December 1 at 11 am", "on December first at eleven a.m."),
    ("We repair roofs for $200 in 3 days.", "We repair roofs for $200 in 3 days."),
])
def test_dates_and_times_go_to_the_voice_in_words(written: str, spoken: str) -> None:
    assert realtime.speakable(written) == spoken


async def test_her_voice_is_asked_for_english_and_hears_the_words() -> None:
    import httpx

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"\xff" * 160)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        voice = realtime.elevenlabs_voice(client, "k", "v", "eleven_flash_v2_5")
        audio = [chunk async for chunk in voice("Thanks, Harshal. That's at 2 PM.")]
    assert audio == [b"\xff" * 160]
    assert seen == [{"text": "Thanks, Harshal. That's at two p.m.",
                     "model_id": "eleven_flash_v2_5", "language_code": "en"}]


async def test_multilingual_v2_is_never_sent_a_language_code() -> None:
    import httpx

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"\xff")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        voice = realtime.elevenlabs_voice(client, "k", "v", "eleven_multilingual_v2")
        _ = [chunk async for chunk in voice("Hi.")]
    assert "language_code" not in seen[0]
