"""ElevenLabs-based ``TTSProvider`` implementation (raw ``httpx``, no SDK).

Mirrors ``api.notifications.providers.TwilioNotificationProvider``'s
raw-HTTP-over-vendor-SDK precedent -- no ``elevenlabs`` package dependency
for one endpoint. Unlike Twilio's notification path this is a synchronous
request-path call (no Celery job to retry), so every failure mode collapses
to one ``VoiceProviderError`` rather than a deterministic/transient split.
"""
from __future__ import annotations

import httpx
from common.logging import get_logger

from api.voice.provider import VoiceProviderError

_log = get_logger(__name__)

_ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"


class ElevenLabsTTSProvider:
    """``TTSProvider`` implementation: ElevenLabs text-to-speech."""

    def __init__(self, *, api_key: str, voice_id: str, model_id: str, timeout: float) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._timeout = timeout

    async def synthesize(self, text: str) -> bytes:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    f"{_ELEVENLABS_API_BASE}/text-to-speech/{self._voice_id}",
                    headers={"xi-api-key": self._api_key, "Content-Type": "application/json"},
                    json={"text": text, "model_id": self._model_id},
                )
            except httpx.HTTPError as exc:
                # Transient (connect error / timeout / read error).
                raise VoiceProviderError("Speech synthesis request failed.") from exc

        if not (200 <= response.status_code < 300):
            _log.warning(
                "voice upstream call failed: provider=elevenlabs op=synthesize status=%s",
                response.status_code,
            )
            raise VoiceProviderError("Speech synthesis request failed.")

        return response.content
