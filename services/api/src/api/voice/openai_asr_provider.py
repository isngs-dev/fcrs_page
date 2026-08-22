"""OpenAI-based ``ASRProvider`` implementation (raw ``openai`` async SDK).

Mirrors ``api.llm.openai_provider.OpenAICompatibleProvider``'s error-mapping
convention: any upstream ``APIError`` becomes a domain error
(``VoiceProviderError``), never lets the SDK exception itself propagate to
the route.
"""
from __future__ import annotations

from typing import Any

from common.logging import get_logger
from openai import APIError

from api.voice.provider import VoiceProviderError

_log = get_logger(__name__)


class OpenAIASRProvider:
    """``ASRProvider`` implementation: OpenAI's audio transcription API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str,
        timeout: float = 20.0,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        self._model = model

    async def transcribe(self, audio: bytes, *, content_type: str) -> str:
        # A synthetic filename is required by the SDK's multipart upload --
        # its extension is read from the recorded blob's own MIME type
        # (e.g. "audio/webm" -> "audio.webm") so the upstream API sees a
        # consistent, correctly-typed file regardless of which codec the
        # visitor's browser actually recorded with.
        extension = content_type.split("/")[-1].split(";")[0] or "webm"
        filename = f"audio.{extension}"
        try:
            resp = await self._client.audio.transcriptions.create(
                model=self._model,
                file=(filename, audio, content_type),
            )
        except APIError as exc:
            _log.warning(
                "voice upstream call failed: provider=openai op=transcribe"
                " model=%s status=%s detail=%s",
                self._model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise VoiceProviderError("Transcription request failed.") from exc

        return str(resp.text).strip()

    async def aclose(self) -> None:
        await self._client.close()
