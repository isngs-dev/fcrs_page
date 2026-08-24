"""ASR/TTS provider boundary -- provider-agnostic Protocols + domain errors.

Both providers are configured via plain environment variables
(``ApiSettings``), NOT per-tenant DB config -- a deliberate, smaller-scope
choice for this integration (env-var-only, one active tenant in practice)
rather than mirroring ``LLMProvider``/``CalendarProvider``'s full per-tenant
encrypted-config machinery. A deployment with no key configured gets an
explicit ``VoiceConfigError``, never a silent fallback -- the WIDGET is what
decides to degrade to its own browser-native mechanism on that error (see the
chat-widget skill), this module only ever tells the truth about its own
configuration state.
"""
from __future__ import annotations

from typing import Protocol

from common.errors import AppException, ValidationError


class ASRProvider(Protocol):
    """Speech-to-text contract. One recorded utterance in, transcript out."""

    async def transcribe(self, audio: bytes, *, content_type: str) -> str: ...

    async def aclose(self) -> None: ...


class TTSProvider(Protocol):
    """Text-to-speech contract. Reply text in, audio bytes out.

    ``speed`` (1.0 = normal) is a synthesis-rate multiplier, not a
    per-tenant setting -- the caller (e.g. a slightly slower greeting)
    decides it per call.
    """

    async def synthesize(self, text: str, *, speed: float = 1.0) -> bytes: ...


class VoiceConfigError(ValidationError):
    """Deterministic config error -- raised before any upstream call."""

    code = "VOICE_PROVIDER_NOT_CONFIGURED"


class VoiceProviderError(AppException):
    """Upstream ASR/TTS provider failure (network, non-2xx, malformed response)."""

    code = "VOICE_PROVIDER_ERROR"
    http_status = 502
    default_message = "Voice request failed."


_SENTENCE_ENDINGS = (".", "!", "?")


def truncate_for_speech(text: str, max_chars: int) -> str:
    """Cap *text* at *max_chars* before it reaches a paid TTS provider.

    Cloud TTS is billed per character (``api.voice.routes.post_speak``'s
    only real cost lever besides the provider/model choice itself) -- this
    is a hard ceiling independent of how well the orchestrator's own
    "keep replies concise" prompt guidance was followed, applied to the
    SPOKEN text only (the displayed chat bubble, from ``/public/chat/
    message``, is never touched here).

    Cuts at the last sentence-ending punctuation (``.``/``!``/``?``) at or
    before the limit, so a truncated utterance still ends on a natural
    pause rather than stopping mid-word. Falls back to the last whitespace
    boundary when no sentence ending exists in range (one long run-on
    sentence), and to a hard character cut only as a last resort.
    """
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    best_end = -1
    for ending in _SENTENCE_ENDINGS:
        idx = window.rfind(ending)
        if idx > best_end:
            best_end = idx
    if best_end != -1:
        return window[: best_end + 1]

    space_idx = window.rfind(" ")
    if space_idx > 0:
        return window[:space_idx].rstrip()

    return window
