"""Speed test (2026-11-07): Rebecca over Plivo two-way audio streaming, not turn-by-turn XML.

Caller audio -> Deepgram Flux (streaming speech-to-text with its own end-of-turn detection)
-> the SAME turn logic as the XML calls (booking, consent, opt-outs, keep-or-reschedule)
-> ElevenLabs streaming voice (mu-law 8 kHz) -> back to Plivo. Talking over her clears her
audio at once (Flux ``StartOfTurn``). Every turn logs where the time went.

Only for tenants listed in ``REALTIME_VOICE_TENANTS`` with both keys set; everyone else
keeps the XML calls. ponytail: platform keys for the test -- per-client keys (encrypted,
like Plivo's) come with the full build. Not yet: transfer to a person (ends the call),
eager end-of-turn, streaming the model's answer sentence by sentence.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx
import websockets
from common.logging import get_logger

from api.config import get_api_settings

_log = get_logger(__name__)

FLUX_URL = "wss://api.deepgram.com/v2/listen"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"
SILENCE_SECONDS = 8.0  # nothing said after she finishes -> a silent turn (as the XML calls)


def enabled_for(tenant_id: str | None) -> bool:
    s = get_api_settings()
    tenants = {t.strip() for t in (s.realtime_voice_tenants or "").split(",") if t.strip()}
    return bool(tenant_id and tenant_id in tenants and s.deepgram_api_key
                and s.elevenlabs_api_key and s.elevenlabs_voice_id)


def stream_xml(call_id: str, ticket: str) -> str:
    """The answer XML: one two-way stream; the call ends when the stream does."""
    base = (get_api_settings().public_api_base_url or "").rstrip("/")
    url = (base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
           + f"/public/call-agent/calls/{call_id}/stream?{urlencode({'ticket': ticket})}")
    return ('<Stream bidirectional="true" keepCallAlive="true" '
            f'contentType="audio/x-mulaw;rate=8000">{escape(url)}</Stream>')


# -- the parts (faked in tests) -------------------------------------------------------------


@dataclass(frozen=True)
class Heard:
    event: str  # Flux TurnInfo event: StartOfTurn, Update, EndOfTurn, ...
    transcript: str


class Socket(Protocol):
    async def receive_text(self) -> str: ...
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


class Listener(Protocol):
    async def send(self, audio: bytes) -> None: ...
    def events(self) -> AsyncIterator[Heard]: ...
    async def close(self) -> None: ...


Voice = Callable[[str], AsyncIterator[bytes]]
SpeakEarly = Callable[[str], Awaitable[None]]  # a finished, checked sentence: say it now
# (speech, speak early) -> (her whole reply, end the call). Sentences already given to
# ``speak early`` are never said twice: only the rest of the reply follows them.
Brain = Callable[[str, SpeakEarly], Awaitable[tuple[str, bool]]]
Record = Callable[[str, int], Awaitable[None]]  # (what she said, ms until her first audio)


def remainder(reply: str, said: list[str]) -> str:
    """What's left of ``reply`` after the sentences already spoken. A reply that no longer
    starts with them (the answer turned into a miss) is said whole."""
    whole, before = " ".join(reply.split()), " ".join(" ".join(said).split())
    return whole[len(before):].strip() if before and whole.startswith(before) else whole


def flux_query(keyterms: list[str]) -> str:
    """Phone audio (mu-law 8 kHz); numbers as digits ("fifteenth" -> "15th", which the date
    reader understands); the business's own name as a keyterm so it's heard right."""
    params: list[tuple[str, str]] = [
        ("model", "flux-general-en"), ("encoding", "mulaw"), ("sample_rate", "8000"),
        ("eot_timeout_ms", "3000"), ("numerals", "true")]
    params += [("keyterm", term) for term in keyterms if term.strip()]
    return urlencode(params)


class FluxListener:
    """Deepgram Flux over its WebSocket: mu-law 8 kHz in, ``TurnInfo`` events out."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    @classmethod
    async def connect(cls, api_key: str, keyterms: list[str]) -> FluxListener:
        ws = await websockets.connect(f"{FLUX_URL}?{flux_query(keyterms)}",
                                      additional_headers={"Authorization": f"Token {api_key}"})
        return cls(ws)

    async def send(self, audio: bytes) -> None:
        await self._ws.send(audio)

    async def events(self) -> AsyncIterator[Heard]:
        async for raw in self._ws:
            message = json.loads(raw)
            if message.get("type") == "TurnInfo":
                yield Heard(str(message.get("event")), str(message.get("transcript") or ""))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        await self._ws.close()


def elevenlabs_voice(client: httpx.AsyncClient, api_key: str, voice_id: str,
                     model: str) -> Voice:
    """ElevenLabs streaming speech, already in the phone's format (mu-law 8 kHz)."""

    async def speak(text: str) -> AsyncIterator[bytes]:
        async with client.stream(
                "POST", ELEVENLABS_URL.format(voice=voice_id),
                params={"output_format": "ulaw_8000"},
                headers={"xi-api-key": api_key},
                json={"text": text, "model_id": model}) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk

    return speak


# -- one call -------------------------------------------------------------------------------


class RealtimeCall:
    """One streamed call. Caller turns run through the turn logic one at a time and in order
    -- never cancelled half-way, so its saved state always matches what was said. Her reply
    starts at its first finished sentence; the rest follows, nothing twice. A reply is
    dropped when the caller has already said more; talking over her stops her audio and
    the rest of that answer."""

    def __init__(self, plivo: Socket, listener: Listener, voice: Voice, brain: Brain, *,
                 opening: str, record: Record, silence_seconds: float = SILENCE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._plivo, self._listener, self._voice, self._brain = plivo, listener, voice, brain
        self._opening, self._record = opening, record
        self._silence_seconds, self._clock = silence_seconds, clock
        self._stream_id = ""
        self._turns: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self._speech: asyncio.Task[None] | None = None  # her audio being sent now
        self._silence: asyncio.Task[None] | None = None
        self._playing: str | None = None  # the checkpoint of what she's saying now
        self._cut_off = False  # talked over: the rest of this answer is dropped
        self._ending = False
        self._replies = 0
        self._done = asyncio.Event()

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._from_plivo()), asyncio.create_task(self._from_flux()),
                 asyncio.create_task(self._think())]
        await self._done.wait()
        for task in [*tasks, self._speech, self._silence]:
            if task is not None:
                task.cancel()
        with contextlib.suppress(Exception):
            await self._listener.close()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _send(self, event: dict[str, Any]) -> None:
        await self._plivo.send_text(json.dumps(event))

    async def _from_plivo(self) -> None:
        try:
            while True:
                message = json.loads(await self._plivo.receive_text())
                event = message.get("event")
                if event == "start":
                    self._stream_id = str(message["start"].get("streamId", ""))
                    lines: asyncio.Queue[str | None] = asyncio.Queue()
                    for line in (self._opening, None):
                        lines.put_nowait(line)
                    started = self._clock()
                    self._speech = asyncio.create_task(
                        self._speak(lines, [self._opening], started, started))
                elif event == "media":
                    await self._listener.send(base64.b64decode(message["media"]["payload"]))
                elif event == "playedStream" and message.get("name") == self._playing:
                    self._playing = None
                    if self._ending:
                        await self._plivo.close()
                        break
                    self._silence = asyncio.create_task(self._wait_silence())
                elif event == "stop":
                    break
        except Exception as exc:  # noqa: BLE001 -- the socket closed: the call is over
            _log.info("realtime call stream closed", extra={
                "event": "realtime_stream_closed", "reason": type(exc).__name__})
        self._done.set()

    async def _from_flux(self) -> None:
        async for heard in self._listener.events():
            if heard.event == "StartOfTurn":
                if self._silence is not None:
                    self._silence.cancel()
                if self._playing is not None or (self._speech and not self._speech.done()):
                    await self._barge_in()
            elif heard.event == "EndOfTurn" and heard.transcript.strip():
                self._turns.put_nowait((heard.transcript.strip(), self._clock()))

    async def _barge_in(self) -> None:
        self._cut_off = True
        if self._speech is not None:
            self._speech.cancel()
        self._playing = None
        await self._send({"event": "clearAudio", "streamId": self._stream_id})

    async def _wait_silence(self) -> None:
        await asyncio.sleep(self._silence_seconds)
        self._turns.put_nowait(("", self._clock()))

    async def _think(self) -> None:
        while True:
            speech, heard_at = await self._turns.get()
            if not await self._answer(speech, heard_at):
                return

    async def _answer(self, speech: str, heard_at: float) -> bool:
        """One caller turn: the turn logic, her reply as it's written. ``False`` = call over."""
        self._cut_off = False
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        said: list[str] = []
        first_line_at: list[float] = []

        def say(line: str) -> None:
            said.append(line)
            lines.put_nowait(line)
            if not first_line_at:
                first_line_at.append(self._clock())
                self._speech = asyncio.create_task(
                    self._speak(lines, said, heard_at, first_line_at[0]))

        async def early(sentence: str) -> None:
            if not self._cut_off and self._turns.empty():  # still their latest words
                say(sentence)

        try:
            text, end = await self._brain(speech, early)
        except Exception as exc:  # noqa: BLE001 -- never dead air: end the call, loudly
            _log.error("realtime turn failed", extra={
                "event": "realtime_turn_failed", "reason": type(exc).__name__})
            await self._plivo.close()
            self._done.set()
            return False
        stale = not self._turns.empty() and not end  # they kept talking
        self._ending = self._ending or end
        rest = "" if stale or self._cut_off else remainder(text, said)
        if rest:
            say(rest)
        if not first_line_at:
            if end:
                await self._plivo.close()
                self._done.set()
                return False
            return True
        lines.put_nowait(None)
        if self._speech is not None:
            await asyncio.wait({self._speech})  # talked over -> cancelled; we carry on
        return True

    async def _speak(self, lines: asyncio.Queue[str | None], said: list[str], heard_at: float,
                     thought_at: float) -> None:
        """Say each queued line as it arrives (``None`` ends the reply), then mark the end
        of the reply so Plivo tells us when it has finished playing."""
        first: float | None = None
        try:
            while (line := await lines.get()) is not None:
                async for chunk in self._voice(line):
                    if first is None:
                        first = self._clock()
                    await self._send({"event": "playAudio", "media": {
                        "contentType": "audio/x-mulaw", "sampleRate": 8000,
                        "payload": base64.b64encode(chunk).decode()}})
        except httpx.HTTPError as exc:  # the voice failed: say so in the logs, end the call
            _log.error("realtime voice failed", extra={
                "event": "realtime_voice_failed", "reason": type(exc).__name__})
            await self._plivo.close()
            self._done.set()
            return
        if first is None:
            return
        self._replies += 1
        self._playing = f"reply-{self._replies}"
        await self._send({"event": "checkpoint", "streamId": self._stream_id,
                          "name": self._playing})
        total = int((first - heard_at) * 1000)
        _log.info("realtime turn timing", extra={
            "event": "realtime_turn",
            "reason": f"logic {int((thought_at - heard_at) * 1000)} ms, "
                      f"voice {int((first - thought_at) * 1000)} ms, total {total} ms"})
        await self._record(" ".join(said), total)
