"""OpenAI-based ``TTSProvider`` implementation (raw ``openai`` async SDK).

Mirrors ``OpenAIASRProvider``'s error-mapping convention exactly -- any
upstream ``APIError`` becomes a domain error (``VoiceProviderError``), never
lets the SDK exception itself propagate to the route. Same OpenAI account/
key as ASR (``api.voice.factory``) -- one provider now covers both
directions.
"""
from __future__ import annotations

from typing import Any

from common.logging import get_logger
from openai import APIError

from api.voice.provider import VoiceProviderError

_log = get_logger(__name__)


class OpenAITTSProvider:
    """``TTSProvider`` implementation: OpenAI's text-to-speech API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str,
        voice: str,
        timeout: float = 20.0,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._voice = voice
        if client is not None:
            self._client = client
        else:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=api_key, timeout=timeout)

    async def synthesize(self, text: str, *, speed: float = 1.0) -> bytes:
        try:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                speed=speed,
            )
        except APIError as exc:
            _log.warning(
                "voice upstream call failed: provider=openai op=synthesize"
                " model=%s status=%s detail=%s",
                self._model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise VoiceProviderError("Speech synthesis request failed.") from exc

        return bytes(response.content)
