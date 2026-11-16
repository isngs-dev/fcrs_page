"""Telnyx as the phone network for Rebecca's streamed calls (T1-T3, 2026-11-16).

Telnyx drives a call with JSON webhooks and one two-way audio WebSocket -- there is no
answer XML. So only three things differ from Plivo, and they all live here:

* **who gets it** (``enabled_for``) -- ponytail: platform settings for the first live calls
  (``TELNYX_*`` + the tenants listed in ``TELNYX_TENANTS``); per-client encrypted keys and
  the provider picker arrive with T4, exactly as the speech keys did;
* **their webhooks** -- the Ed25519 signature check and their events mapped onto the ones the
  call logic already handles (answered / machine / hangup / recording saved);
* **their stream dialect** -- ``TelnyxSocket`` translates it both ways, so ``RealtimeCall``
  (turn order, barge-in, the closing line) runs unchanged.

Facts verified against Telnyx's OpenAPI spec + SDK source: specs/calling_agent.md §Telnyx.
"""
from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from common.errors import AuthenticationError
from common.logging import get_logger
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from api.call_agent import realtime
from api.config import get_api_settings

_log = get_logger(__name__)

SIGNATURE_HEADER = "telnyx-signature-ed25519"
TIMESTAMP_HEADER = "telnyx-timestamp"
TOLERANCE_SECONDS = 300  # Telnyx's SDK default: older (or future-dated) events are replays
FRAME = 160  # 20 ms of 8 kHz mu-law: the smallest chunk Telnyx's stream accepts
_SILENCE = b"\xff"  # mu-law silence, pads the last frame of a reply


class TelnyxSignatureInvalidError(AuthenticationError):
    """Missing/mismatched Telnyx signature, or an event for no known call (401).
    One code for every rejection mode -- never leaks which check failed."""

    code = "TELNYX_SIGNATURE_INVALID"
    default_message = "The Telnyx webhook signature is missing or invalid."


def enabled_for(tenant_id: str | None) -> bool:
    """This tenant's calls go through Telnyx. Telnyx calls are streamed calls, so the
    streaming setup (speech keys) must be on for the tenant too."""
    s = get_api_settings()
    tenants = {t.strip() for t in (s.telnyx_tenants or "").split(",") if t.strip()}
    return bool(tenant_id and tenant_id in tenants and s.telnyx_api_key and s.telnyx_public_key
                and s.telnyx_connection_id and s.telnyx_from_number
                and realtime.enabled_for(tenant_id))


def _base() -> str:
    return (get_api_settings().public_api_base_url or "").rstrip("/")


def stream_url(call_id: str, ticket: str) -> str:
    """The call's audio stream: the same route Plivo's stream uses."""
    return (_base().replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            + f"/public/call-agent/calls/{call_id}/stream?{urlencode({'ticket': ticket})}")


def events_url(call_id: str, ticket: str) -> str:
    """Where Telnyx posts this call's events (set per call when dialling)."""
    return (f"{_base()}/public/call-agent/telnyx/calls/{call_id}/events"
            f"?{urlencode({'ticket': ticket})}")


# -- webhooks ---------------------------------------------------------------------------------


def verify_signature(*, public_key: str, body: bytes, signature: str | None,
                     timestamp: str | None, now: float | None = None,
                     tolerance: int = TOLERANCE_SECONDS) -> None:
    """Ed25519 over ``<timestamp>|<raw body>``; key and signature are base64 (Telnyx SDK)."""
    try:
        if not (public_key and signature and timestamp):
            raise ValueError("missing")
        if abs((time.time() if now is None else now) - int(timestamp)) > tolerance:
            raise ValueError("outside the replay window")
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), timestamp.encode() + b"|" + body)
    except (ValueError, binascii.Error, InvalidSignature) as exc:
        raise TelnyxSignatureInvalidError() from exc


@dataclass(frozen=True)
class Event:
    kind: str  # answered | machine | hangup | recording_saved | stream_failed | other
    call_control_id: str = ""
    event_id: str = ""
    params: dict[str, str] = field(default_factory=dict)  # a hangup, as on_hangup reads it
    recording_id: str | None = None
    reason: str = ""


# Their hangup causes -> the call statuses the call logic already settles by.
_HANGUP_STATUS = {
    "normal_clearing": "completed", "time_limit": "completed",
    "timeout": "no-answer", "no_answer": "no-answer",
    "user_busy": "busy", "call_rejected": "busy", "originator_cancel": "cancel",
}


def parse_event(body: bytes) -> Event:
    """One Telnyx event -> ours. Anything unknown or malformed is ``other`` (acknowledged and
    ignored: Telnyx sends many event types, in no guaranteed order, sometimes twice)."""
    try:
        data = json.loads(body)["data"]
        event_type, payload = str(data["event_type"]), data["payload"]
        control_id = str(payload["call_control_id"])
    except (ValueError, KeyError, TypeError):
        return Event(kind="other")
    common: dict[str, Any] = {"call_control_id": control_id, "event_id": str(data.get("id") or "")}
    if event_type == "call.answered":
        return Event(kind="answered", **common)
    if event_type == "call.machine.detection.ended" and payload.get("result") == "machine":
        return Event(kind="machine", **common)
    if event_type == "call.hangup":
        cause = str(payload.get("hangup_cause") or "")
        return Event(kind="hangup", **common, params={
            "CallStatus": _HANGUP_STATUS.get(cause, "failed"),
            "HangupSource": str(payload.get("hangup_source") or ""),
            "HangupCauseName": cause, "CallUUID": control_id})
    if event_type == "call.recording.saved":
        recording_id = payload.get("recording_id")
        return Event(kind="recording_saved", **common,
                     recording_id=str(recording_id) if recording_id else None)
    if event_type == "streaming.failed":
        return Event(kind="stream_failed", **common,
                     reason=str(payload.get("failure_reason") or ""))
    return Event(kind="other", **common)


# -- the stream dialect -----------------------------------------------------------------------


class _Ws(Protocol):
    async def receive_text(self) -> str: ...
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


class TelnyxSocket:
    """Telnyx's stream messages <-> the ones ``RealtimeCall`` speaks (Plivo's names).

    In: ``start``/``media``/``mark``/``stop`` -> ``start``/``media``/``playedStream``/``stop``
    (only the caller's track; ``connected``, ``dtmf`` and ``error`` are skipped, errors logged).
    Out: ``playAudio`` -> ``media`` in whole 20 ms frames (the voice hands over any size; the
    remainder is held and padded with silence at the reply's end), ``checkpoint`` -> ``mark``,
    ``clearAudio`` -> ``clear`` (and the held remainder is dropped)."""

    def __init__(self, ws: _Ws) -> None:
        self._ws = ws
        self._held = b""

    async def receive_text(self) -> str:
        while True:
            message = json.loads(await self._ws.receive_text())
            event = message.get("event")
            if event == "start":
                start = message.get("start") or {}
                return json.dumps({"event": "start", "start": {
                    "streamId": str(message.get("stream_id") or ""),
                    "callId": str(start.get("call_control_id") or "")}})
            if event == "media":
                media = message.get("media") or {}
                if media.get("track", "inbound") != "inbound":  # never her own voice
                    continue
                return json.dumps({"event": "media", "media": {"payload": media.get("payload")}})
            if event == "mark":
                return json.dumps({"event": "playedStream",
                                   "name": (message.get("mark") or {}).get("name")})
            if event == "stop":
                return json.dumps({"event": "stop"})
            if event == "error":
                detail = message.get("payload") or {}
                _log.warning("telnyx stream error", extra={
                    "event": "telnyx_stream_error",
                    "reason": f"{detail.get('code')}: {detail.get('title')}"})

    async def _media(self, audio: bytes) -> None:
        await self._ws.send_text(json.dumps({"event": "media", "media": {
            "payload": base64.b64encode(audio).decode()}}))

    async def send_text(self, data: str) -> None:
        message = json.loads(data)
        event = message.get("event")
        if event == "playAudio":
            self._held += base64.b64decode(message["media"]["payload"])
            whole = len(self._held) - len(self._held) % FRAME
            if whole:
                audio, self._held = self._held[:whole], self._held[whole:]
                await self._media(audio)
        elif event == "checkpoint":
            if self._held:
                audio = self._held + _SILENCE * (-len(self._held) % FRAME)
                self._held = b""
                await self._media(audio)
            await self._ws.send_text(json.dumps({"event": "mark",
                                                 "mark": {"name": message.get("name")}}))
        elif event == "clearAudio":
            self._held = b""
            await self._ws.send_text(json.dumps({"event": "clear"}))

    async def close(self, code: int = 1000) -> None:
        await self._ws.close(code)
