"""TelephonyProvider (A6 L2) + Plivo implementation over raw httpx.

Deterministic failures (bad credentials, rejected number) raise
``CallProviderError`` (a ``ValidationError``, never retried); network errors and
5xx/429 raise ``TransientExecutorError`` (the dispatcher retries with backoff).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from common.errors import ValidationError

from api.agents.executors import TransientExecutorError
from api.voice_agent.plivo import PLIVO_API_BASE


class CallProviderError(ValidationError):
    code = "CALL_PROVIDER_REJECTED"


@dataclass(frozen=True)
class PlacedCall:
    request_uuid: str


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
