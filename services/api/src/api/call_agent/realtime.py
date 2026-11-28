"""Speed test (2026-11-07): Rebecca over Plivo two-way audio streaming, not turn-by-turn XML.

Caller audio -> Deepgram Flux (streaming speech-to-text with its own end-of-turn detection)
-> the SAME turn logic as the XML calls (booking, consent, opt-outs, keep-or-reschedule)
-> ElevenLabs streaming voice (mu-law 8 kHz) -> back to Plivo. Talking over her clears her
audio at once (Flux ``StartOfTurn``). Every turn logs where the time went.

Only for tenants listed in ``REALTIME_VOICE_TENANTS`` with both keys set; everyone else
keeps the XML calls. ponytail: platform keys for the test -- per-client keys (encrypted,
like Plivo's) come with the full build. Not yet: transfer to a person (ends the call).
Since 2026-11-12 the answer streams sentence by sentence; since 2026-11-19 Flux's eager
end-of-turn starts the knowledge lookup early (the turn itself still waits for EndOfTurn);
and the opening waits for their hello, or a 2 s silence, so none of it is missed.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx
import websockets
from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger

from api.call_agent.script import is_voicemail_greeting
from api.config import get_api_settings
from api.llm.config_repository import LLMConfig, get_llm_config

_log = get_logger(__name__)

FLUX_URL = "wss://api.deepgram.com/v2/listen"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"
SILENCE_SECONDS = 8.0  # nothing said after she finishes -> a silent turn (as the XML calls)
# Live 2026-11-19: the line counted as answered before the person could hear (CallHippo's
# app), and a "Hello?" over the opening cleared it -- they heard only "...is now a good
# time?". She now waits for their hello, or this long without a word, then says it all.
OPENING_WAIT = 2.0
# Live 2026-11-20: every "hello?" or bit of line noise restarted that wait -- 12-15 s of
# silence. However much they talk, the opening starts this soon after the call is answered.
OPENING_CAP = 4.0
# A turn that ends mid-sentence ("So", "I was saying that") waits this long for the rest.
FRAGMENT_WAIT = 1.5
_GREETING = {"hello", "hi", "hey", "hallo", "yes", "yeah", "yep", "ok", "okay", "haan"}
_DANGLING = {"so", "and", "but", "because", "that", "the", "a", "an", "to", "of", "or", "if",
             "like", "um", "uh", "umm", "er", "i", "my", "with", "for", "about", "is", "was",
             "in", "on", "at", "mean", "well"}


def _words(text: str) -> list[str]:
    return [w for w in "".join(c if c.isalnum() or c in " '" else " "
                               for c in text.lower()).split() if w]


def is_cut_off(text: str) -> bool:
    """Ends mid-sentence ("So", "I was saying that", "and the") -- more is coming."""
    words = _words(text)
    return bool(words) and words[-1] in _DANGLING and not text.rstrip().endswith("?")


def _just_hello(text: str) -> bool:
    words = _words(text)
    return bool(words) and len(words) <= 3 and all(w in _GREETING for w in words)


def enabled_for(tenant_id: str | None) -> bool:
    s = get_api_settings()
    tenants = {t.strip() for t in (s.realtime_voice_tenants or "").split(",") if t.strip()}
    listed = "*" in tenants or tenant_id in tenants  # "*": every chatbot (owner, 2026-11-28)
    return bool(tenant_id and listed and s.deepgram_api_key
                and s.elevenlabs_api_key and s.elevenlabs_voice_id)


async def calls_llm(db: Database, claims: AuthClaims) -> LLMConfig | None:
    """The AI for this tenant's streamed calls when the server sets one (Claude via
    ``ANTHROPIC_API_KEY`` + ``CALLS_LLM_MODEL``), else ``None`` -> the tenant's own setting.
    The tenant's embeddings settings are kept: its knowledge base was embedded there.
    ponytail: one platform key for the speed test; per-client keys with the full build."""
    s = get_api_settings()
    if not (s.anthropic_api_key and s.calls_llm_model and enabled_for(claims.tenant_id)):
        return None
    tenant = await get_llm_config(db, claims)
    if tenant is None:
        return None
    return replace(tenant, provider="anthropic", model=s.calls_llm_model,
                   api_key=s.anthropic_api_key, base_url=None, api_version=None)


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
# Said only when an AI answer is slow to start (owner, 2026-11-16): neutral words -- "Right."
# or "Okay." before "No, we don't do that" sounded like a yes. Rotated, never twice in a row.
# Not "Good question" (the miss line says it).
BRIDGES = ("Let me check.", "One moment.", "Let me see.")
BRIDGE_DELAY = 1.2  # seconds; Claude Haiku's first sentence is ~1.0 s, so most answers need none


class Speaking(Protocol):
    """This turn's early voice: ``sentence`` = a finished, checked sentence, say it now;
    ``bridge`` = a short word while the answer is worked out (never part of the answer)."""

    async def sentence(self, text: str) -> None: ...

    async def bridge(self) -> None: ...


class _TurnVoice:
    def __init__(self, sentence: Callable[[str], Awaitable[None]],
                 bridge: Callable[[], Awaitable[None]]) -> None:
        self._sentence, self._bridge = sentence, bridge

    async def sentence(self, text: str) -> None:
        await self._sentence(text)

    async def bridge(self) -> None:
        await self._bridge()


# (speech, this turn's voice) -> (her whole reply, end the call). Sentences already said
# early are never said twice: only the rest of the reply follows them.
Brain = Callable[[str, Speaking], Awaitable[tuple[str, bool]]]
Record = Callable[[str, int], Awaitable[None]]  # (what she said, ms until her first audio)


def remainder(reply: str, said: list[str]) -> str:
    """What's left of ``reply`` after the sentences already spoken. A reply that no longer
    starts with them (the answer turned into a miss) is said whole."""
    whole, before = " ".join(reply.split()), " ".join(" ".join(said).split())
    return whole[len(before):].strip() if before and whole.startswith(before) else whole


def keyterms(company: str | None) -> list[str]:
    """Words Flux should listen out for: the business's name + ``DEEPGRAM_KEYTERMS`` (the
    trade's own words -- "roofs" was heard as "rupees"). ponytail: one platform list for the
    speed test; per-client terms come with per-client speech settings."""
    configured = (get_api_settings().deepgram_keyterms or "").split(",")
    return [t.strip() for t in [company or "", *configured] if t.strip()]


def flux_query(keyterms: list[str]) -> str:
    """Phone audio (mu-law 8 kHz); numbers as digits ("fifteenth" -> "15th", which the date
    reader understands); the business's own name as a keyterm so it's heard right."""
    params: list[tuple[str, str]] = [
        ("model", "flux-general-en"), ("encoding", "mulaw"), ("sample_rate", "8000"),
        ("eot_timeout_ms", "3000"), ("numerals", "true"),
        # Live 2026-11-20: 0.8 was rarely reached on Indian lines -- every turn waited out the
        # timeout and replies came late. Flux's 0.7; a cut-off turn is joined in code instead
        # (``is_cut_off``).
        ("eot_threshold", "0.7"),
        # 2026-11-19: an early "they seem done" (EagerEndOfTurn) starts the knowledge lookup
        # while Flux makes sure; must stay below eot_threshold.
        ("eager_eot_threshold", "0.5")]
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
                 bridge_delay: float = BRIDGE_DELAY,
                 clock: Callable[[], float] = time.monotonic,
                 prefetch: Callable[[str], None] | None = None,
                 opening_wait: float = OPENING_WAIT, opening_cap: float = OPENING_CAP,
                 fragment_wait: float = FRAGMENT_WAIT) -> None:
        self._plivo, self._listener, self._voice, self._brain = plivo, listener, voice, brain
        self._prefetch = prefetch  # EagerEndOfTurn words: look the answer up early
        self._opening, self._record = opening, record
        self._silence_seconds, self._clock = silence_seconds, clock
        self._bridge_delay = bridge_delay
        self._opening_wait, self._opening_cap = opening_wait, opening_cap
        self._opened = False  # the opening started (or a voicemail greeting took its place)
        self._waiting: asyncio.Task[None] | None = None  # the short wait for their hello
        self._deadline: asyncio.Task[None] | None = None  # the opening starts by then anyway
        self._opening_task: asyncio.Task[None] | None = None  # sending the opening
        self._opening_mark: str | None = None  # its checkpoint, until it has played
        self._fragment_wait = fragment_wait
        self._fragment = ""  # a cut-off turn waiting for the rest
        self._holding: asyncio.Task[None] | None = None
        self._stream_id = ""
        self._turns: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self._speech: asyncio.Task[None] | None = None  # her audio being sent now
        self._silence: asyncio.Task[None] | None = None
        self._playing: str | None = None  # the checkpoint of what she's saying now
        self._cut_off = False  # talked over: the rest of this answer is dropped
        self._ending = False
        self._replies = 0
        self._bridges = 0
        self._frames = 0  # caller audio frames passed to the listener
        self._heard = 0  # listener events (any kind) -- with _frames, says who went quiet
        self._last_heard: float | None = None
        self._started = clock()
        self._done = asyncio.Event()

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._from_plivo()), asyncio.create_task(self._from_flux()),
                 asyncio.create_task(self._think())]
        await self._done.wait()
        for task in [*tasks, self._speech, self._silence, self._waiting, self._deadline,
                     self._holding]:
            if task is not None:
                task.cancel()
        with contextlib.suppress(Exception):
            await self._listener.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Live 2026-11-28: the caller went unheard mid-call. This line says whether their
        # audio was still arriving (frames) and when the listener last reported anything.
        last = ("never" if self._last_heard is None
                else f"{int(self._last_heard - self._started)} s in")
        _log.info("realtime call finished", extra={
            "event": "realtime_call_finished",
            "reason": f"{self._frames} caller frames, {self._heard} listener events, "
                      f"last {last}, {int(self._clock() - self._started)} s long"})

    async def _send(self, event: dict[str, Any]) -> None:
        await self._plivo.send_text(json.dumps(event))

    async def _from_plivo(self) -> None:
        try:
            while True:
                message = json.loads(await self._plivo.receive_text())
                event = message.get("event")
                if event == "start":
                    self._stream_id = str(message["start"].get("streamId", ""))
                    self._waiting = asyncio.create_task(self._open_after(self._opening_wait))
                    self._deadline = asyncio.create_task(self._open_after(self._opening_cap))
                elif event == "media":
                    await self._listener.send(base64.b64decode(message["media"]["payload"]))
                    self._frames += 1
                elif event == "playedStream" and message.get("name") == self._playing:
                    self._playing = None
                    if message.get("name") == self._opening_mark:
                        self._opening_mark = None  # the opening has been heard
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

    async def _open_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._open()

    def _open(self) -> None:
        """Say the opening, once."""
        if self._opened:
            return
        self._opened = True
        self._stop_waiting()
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        for line in (self._opening, None):
            lines.put_nowait(line)
        started = self._clock()
        self._opening_mark = f"reply-{self._replies + 1}"
        self._speech = asyncio.create_task(self._speak(lines, [self._opening], started, started))
        self._opening_task = self._speech

    def _stop_waiting(self) -> None:
        for timer in (self._waiting, self._deadline):
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()

    def _in_opening(self) -> bool:
        """Her opening is still going out or still playing: it isn't talked over (B)."""
        return self._opening_task is not None and (
            not self._opening_task.done() or self._opening_mark is not None)

    def _before_opening(self, heard: Heard) -> None:
        """Before she has spoken: their hello is heard out (never a turn), then the opening;
        a voicemail greeting goes to the turn logic instead (it leaves the message)."""
        if heard.event == "StartOfTurn" and self._waiting is not None:
            self._waiting.cancel()  # they're talking: let them finish -- the cap still holds
        elif heard.event == "EndOfTurn":
            if is_voicemail_greeting(heard.transcript):
                self._opened = True
                self._stop_waiting()
                self._turns.put_nowait((heard.transcript.strip(), self._clock()))
            else:
                self._open()

    async def _from_flux(self) -> None:
        """The listener's events. If it stops while the call is up she can no longer hear
        them: the call ends, loudly, rather than going on deaf (live 2026-11-28)."""
        reason = "ended"
        try:
            await self._listen()
        except Exception as exc:  # noqa: BLE001 -- logged below and the call ends
            reason = type(exc).__name__
        if not self._done.is_set():
            _log.error("realtime listener stopped mid-call", extra={
                "event": "realtime_listener_lost", "reason": reason})
            with contextlib.suppress(Exception):
                await self._plivo.close()
            self._done.set()

    async def _listen(self) -> None:
        async for heard in self._listener.events():
            self._heard += 1
            self._last_heard = self._clock()
            if self._ending:  # her closing line plays out whole; nothing after it is a turn
                continue
            if not self._opened:
                self._before_opening(heard)
                continue
            if heard.event == "StartOfTurn":
                if self._silence is not None:
                    self._silence.cancel()
                if self._holding is not None:  # they're carrying on: the cut-off waits for it
                    self._holding.cancel()
                if self._in_opening():
                    continue  # the opening is never talked over (B)
                if self._playing is not None or (self._speech and not self._speech.done()):
                    await self._barge_in()
            elif heard.event == "EagerEndOfTurn" and heard.transcript.strip():
                if self._prefetch is not None:  # not a turn: they may carry on (TurnResumed)
                    self._prefetch(heard.transcript.strip())
            elif heard.event == "EndOfTurn" and heard.transcript.strip():
                said = heard.transcript.strip()
                if self._in_opening() and _just_hello(said) and not self._fragment:
                    continue  # "Hello?" over the opening: it's answering them already
                self._end_of_turn(said)

    def _end_of_turn(self, said: str) -> None:
        """Their turn -- or, if it stops mid-sentence, held a moment for the rest (C)."""
        if self._holding is not None:
            self._holding.cancel()
        said = f"{self._fragment} {said}".strip() if self._fragment else said
        if is_cut_off(said):
            self._fragment = said
            self._holding = asyncio.create_task(self._release_fragment())
            return
        self._fragment = ""
        self._turns.put_nowait((said, self._clock()))

    async def _release_fragment(self) -> None:
        await asyncio.sleep(self._fragment_wait)
        said, self._fragment = self._fragment, ""
        if said:  # nothing more came: it's their turn as it is
            self._turns.put_nowait((said, self._clock()))

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
        if self._opening_task is not None and not self._opening_task.done():
            await asyncio.wait({self._opening_task})  # her reply follows the whole opening
        self._cut_off = False
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        said: list[str] = []
        first_line_at: list[float] = []

        def say(line: str, *, part_of_answer: bool = True) -> None:
            if part_of_answer:  # a bridge is neither repeated-against nor transcribed
                said.append(line)
            lines.put_nowait(line)
            if not first_line_at:
                first_line_at.append(self._clock())
                self._speech = asyncio.create_task(
                    self._speak(lines, said, heard_at, first_line_at[0]))

        def still_theirs() -> bool:
            return not self._cut_off and self._turns.empty()  # their latest words, not cut off

        async def sentence(text: str) -> None:
            if still_theirs():
                say(text)

        bridging: list[asyncio.Task[None]] = []

        async def bridge_if_slow() -> None:
            await asyncio.sleep(self._bridge_delay)
            if still_theirs() and not first_line_at:  # the answer still hasn't started
                say(BRIDGES[self._bridges % len(BRIDGES)], part_of_answer=False)
                self._bridges += 1

        async def bridge() -> None:
            if not bridging and still_theirs() and not first_line_at:
                bridging.append(asyncio.create_task(bridge_if_slow()))

        try:
            text, end = await self._brain(speech, _TurnVoice(sentence, bridge))
        except Exception as exc:  # noqa: BLE001 -- never dead air: end the call, loudly
            _log.error("realtime turn failed", extra={
                "event": "realtime_turn_failed", "reason": type(exc).__name__})
            await self._plivo.close()
            self._done.set()
            return False
        finally:
            for task in bridging:  # the answer is in: a bridge now would only be late
                task.cancel()
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
            if not speech and self._turns.empty() and not self._cut_off:
                # nothing to say to a silence (they asked her to hold): keep the silence
                # clock running, or the next check-in would never come
                self._silence = asyncio.create_task(self._wait_silence())
            return True
        lines.put_nowait(None)
        if self._speech is not None:
            await asyncio.wait({self._speech})  # talked over -> cancelled; we carry on
        return not end  # after her closing line: hang up when it has played, no more turns

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
