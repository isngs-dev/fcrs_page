"""TelephonyProvider (A6 L2) + Plivo implementation over raw httpx.

Deterministic failures (bad credentials, rejected number) raise
``CallProviderError`` (a ``ValidationError``, never retried); network errors and
5xx/429 raise ``TransientExecutorError`` (the dispatcher retries with backoff).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from common.errors import ValidationError

from api.agents.executors import TransientExecutorError
from api.voice_agent.plivo import PLIVO_API_BASE


class CallProviderError(ValidationError):
    code = "CALL_PROVIDER_REJECTED"


@dataclass(frozen=True)
class PlacedCall:
    request_uuid: str
    call_uuid: str | None = None  # known at once on Telnyx; Plivo sends it on answer


@dataclass(frozen=True)
class Recording:
    recording_id: str | None
    url: str | None


class TelephonyProvider(Protocol):
    async def place_call(self, *, to_e164: str, from_e164: str, answer_url: str, hangup_url: str,
                         machine_url: str, time_limit: int) -> PlacedCall: ...

    async def start_recording(self, *, call_uuid: str, time_limit: int) -> Recording: ...

    async def stop_recording(self, *, call_uuid: str) -> None: ...

    async def delete_recording(self, *, recording_id: str) -> None: ...

    async def transfer_to_url(self, *, call_uuid: str, url: str) -> None: ...

    async def fetch_recording(self, *, url: str) -> bytes: ...


class PlivoTelephony:
    def __init__(self, auth_id: str, auth_token: str, *, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._auth = (auth_id, auth_token)
        self._base = f"{PLIVO_API_BASE}/Account/{auth_id}"
        self._timeout = timeout
        self._transport = transport

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None
                       ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                response = await c.request(method, f"{self._base}{path}", auth=self._auth,
                                           json=json)
        except httpx.HTTPError as exc:
            raise TransientExecutorError(f"Plivo unreachable: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientExecutorError(f"Plivo HTTP {response.status_code}")
        if response.status_code >= 400:
            raise CallProviderError(f"Plivo rejected the request (HTTP {response.status_code}).",
                                    code=f"PLIVO_{response.status_code}")
        if not response.content:
            return {}
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def place_call(self, *, to_e164: str, from_e164: str, answer_url: str, hangup_url: str,
                         machine_url: str, time_limit: int) -> PlacedCall:
        body = await self._request("POST", "/Call/", {
            "from": from_e164.lstrip("+"), "to": to_e164.lstrip("+"),
            "answer_url": answer_url, "answer_method": "POST",
            "hangup_url": hangup_url, "hangup_method": "POST",
            # Asynchronous answering-machine detection (L7): Plivo posts Machine=true later.
            "machine_detection": "true", "machine_detection_time": 5000,
            "machine_detection_url": machine_url, "machine_detection_method": "POST",
            "time_limit": time_limit, "ring_timeout": 30,
        })
        uuid = body.get("request_uuid")
        if isinstance(uuid, list):
            uuid = uuid[0] if uuid else None
        if not uuid:
            raise CallProviderError("Plivo did not return a call id.", code="PLIVO_NO_REQUEST_UUID")
        return PlacedCall(request_uuid=str(uuid))

    async def start_recording(self, *, call_uuid: str, time_limit: int) -> Recording:
        body = await self._request("POST", f"/Call/{call_uuid}/Record/",
                                   {"time_limit": time_limit, "file_format": "mp3"})
        return Recording(recording_id=body.get("recording_id"), url=body.get("url"))

    async def stop_recording(self, *, call_uuid: str) -> None:
        await self._request("DELETE", f"/Call/{call_uuid}/Record/")

    async def delete_recording(self, *, recording_id: str) -> None:
        await self._request("DELETE", f"/Recording/{recording_id}/")

    async def transfer_to_url(self, *, call_uuid: str, url: str) -> None:
        await self._request("POST", f"/Call/{call_uuid}/",
                            {"legs": "aleg", "aleg_url": url, "aleg_method": "POST"})

    async def fetch_recording(self, *, url: str) -> bytes:
        """The recording's mp3 (A8 U6). The caller only passes Plivo URLs."""
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport,
                                         follow_redirects=True) as c:
                response = await c.get(url, auth=self._auth)
        except httpx.HTTPError as exc:
            raise TransientExecutorError(f"Plivo unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise CallProviderError(f"Plivo refused the recording (HTTP {response.status_code}).",
                                    code=f"PLIVO_{response.status_code}")
        return response.content


TELNYX_API_BASE = "https://api.telnyx.com/v2"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
_PENDING = "pending:"  # a recording Telnyx hasn't named yet: "this call's recording(s)"
_TELNYX_URL = "telnyx:"  # how a Telnyx recording is stored: by id (its download links expire)


class TelnyxTelephony:
    """Telnyx's Voice API (T1): a call is placed with its event URL and its two-way audio
    stream (phone audio, mu-law 8 kHz); the stream starts when the call is answered.
    The same failure contract as Plivo: a refusal is final, an outage is retried."""

    def __init__(self, api_key: str, connection_id: str, *, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._connection_id = connection_id
        self._timeout = timeout
        self._transport = transport

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None,
                       params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                response = await c.request(method, f"{TELNYX_API_BASE}{path}", json=json,
                                           params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise TransientExecutorError(f"Telnyx unreachable: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientExecutorError(f"Telnyx HTTP {response.status_code}")
        if response.status_code >= 400:
            raise CallProviderError(
                f"Telnyx rejected the request (HTTP {response.status_code}).",
                code=f"TELNYX_{response.status_code}")
        if not response.content:
            return {}
        body = response.json()
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _call(call_uuid: str, action: str) -> str:
        return f"/calls/{quote(call_uuid, safe=':')}/actions/{action}"

    async def dial(self, *, to_e164: str, from_e164: str, webhook_url: str, stream_url: str,
                   time_limit: int) -> PlacedCall:
        body = await self._request("POST", "/calls", {
            "connection_id": self._connection_id, "to": to_e164, "from": from_e164,
            "webhook_url": webhook_url, "timeout_secs": 30, "time_limit_secs": time_limit,
            # Asynchronous: Telnyx posts call.machine.detection.ended while the call goes on.
            "answering_machine_detection": "detect",
            "stream_url": stream_url, "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU",
        })
        data = _dict(body.get("data"))
        control_id = data.get("call_control_id")
        if not control_id:
            raise CallProviderError("Telnyx did not return a call id.",
                                    code="TELNYX_NO_CALL_CONTROL_ID")
        return PlacedCall(request_uuid=str(data.get("call_leg_id") or control_id),
                          call_uuid=str(control_id))

    async def place_call(self, *, to_e164: str, from_e164: str, answer_url: str, hangup_url: str,
                         machine_url: str, time_limit: int) -> PlacedCall:
        raise CallProviderError("Telnyx calls are placed with dial().", code="TELNYX_UNSUPPORTED")

    async def hangup(self, *, call_uuid: str) -> None:
        await self._request("POST", self._call(call_uuid, "hangup"), {})

    @staticmethod
    def _state(value: str) -> str:
        return base64.b64encode(value.encode()).decode()

    async def answer(self, *, call_uuid: str, client_state: str) -> None:
        await self._request("POST", self._call(call_uuid, "answer"),
                            {"client_state": self._state(client_state)})

    async def reject(self, *, call_uuid: str) -> None:
        await self._request("POST", self._call(call_uuid, "reject"), {"cause": "CALL_REJECTED"})

    async def speak(self, *, call_uuid: str, text: str, client_state: str) -> None:
        from api.call_agent.telnyx import CALLBACK_VOICE  # noqa: PLC0415 -- telnyx imports us

        await self._request("POST", self._call(call_uuid, "speak"), {
            "payload": text, "voice": CALLBACK_VOICE, "client_state": self._state(client_state)})

    async def gather_using_speak(self, *, call_uuid: str, text: str, client_state: str) -> None:
        """Say ``text`` and wait up to 8 s for one key: only "1" counts."""
        from api.call_agent.telnyx import CALLBACK_VOICE  # noqa: PLC0415

        await self._request("POST", self._call(call_uuid, "gather_using_speak"), {
            "payload": text, "voice": CALLBACK_VOICE, "valid_digits": "1", "minimum_digits": 1,
            "maximum_digits": 1, "timeout_millis": 8000, "maximum_tries": 1,
            "client_state": self._state(client_state)})

    async def transfer(self, *, call_uuid: str, to_e164: str, from_e164: str) -> None:
        await self._request("POST", self._call(call_uuid, "transfer"),
                            {"to": to_e164, "from": from_e164})

    async def transfer_to_url(self, *, call_uuid: str, url: str) -> None:
        raise CallProviderError("Telnyx has no XML transfer; use transfer().",
                                code="TELNYX_UNSUPPORTED")

    async def start_recording(self, *, call_uuid: str, time_limit: int) -> Recording:
        await self._request("POST", self._call(call_uuid, "record_start"),
                            {"format": "mp3", "channels": "single", "max_length": time_limit})
        return Recording(recording_id=f"{_PENDING}{call_uuid}", url=None)

    async def stop_recording(self, *, call_uuid: str) -> None:
        await self._request("POST", self._call(call_uuid, "record_stop"), {})

    async def recording_ids(self, *, call_uuid: str) -> list[str]:
        body = await self._request("GET", "/recordings",
                                   params={"filter[call_control_id]": call_uuid})
        found = body.get("data")
        rows: list[Any] = found if isinstance(found, list) else []
        return [str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")]

    async def delete_recording(self, *, recording_id: str) -> None:
        ids = (await self.recording_ids(call_uuid=recording_id[len(_PENDING):])
               if recording_id.startswith(_PENDING) else [recording_id])
        for one in ids:
            await self._request("DELETE", f"/recordings/{quote(one, safe='')}")

    async def fetch_recording(self, *, url: str) -> bytes:
        """``telnyx:<recording id>`` -> a fresh download link from Telnyx's API -> the mp3.
        Only a link Telnyx's own (authenticated) API returned is fetched, without our key."""
        if not url.startswith(_TELNYX_URL):
            raise CallProviderError("Not a Telnyx recording.", code="TELNYX_NOT_A_RECORDING")
        body = await self._request("GET", f"/recordings/{quote(url[len(_TELNYX_URL):], safe='')}")
        data = _dict(body.get("data"))
        link = _dict(data.get("download_urls")).get("mp3")
        if not link or not str(link).startswith("https://"):
            raise CallProviderError("Telnyx has no file for this recording.",
                                    code="TELNYX_NO_RECORDING_FILE")
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport,
                                         follow_redirects=True) as c:
                response = await c.get(str(link))
        except httpx.HTTPError as exc:
            raise TransientExecutorError(f"Telnyx unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise CallProviderError(
                f"Telnyx refused the recording (HTTP {response.status_code}).",
                code=f"TELNYX_{response.status_code}")
        return response.content
