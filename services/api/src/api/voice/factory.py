"""Provider selection from ``ApiSettings`` env vars (no per-tenant config).

Mirrors ``api.scheduling.calendar.calendar_provider_for`` /
``api.notifications.providers.notification_provider_for``'s shape: raise a
deterministic config error before any network call, never construct a
provider against a missing key. Both ASR and TTS are OpenAI -- one account,
one key (``openai_api_key``), unlike the ElevenLabs-for-TTS split this
replaced.
"""
from __future__ import annotations

from api.config import ApiSettings
from api.voice.openai_asr_provider import OpenAIASRProvider
from api.voice.openai_tts_provider import OpenAITTSProvider
from api.voice.provider import ASRProvider, TTSProvider, VoiceConfigError


def asr_provider_for(settings: ApiSettings) -> ASRProvider:
    if not settings.openai_api_key:
        raise VoiceConfigError(
            "OPENAI_API_KEY is not configured.",
            code="VOICE_PROVIDER_NOT_CONFIGURED",
        )
    return OpenAIASRProvider(
        api_key=settings.openai_api_key,
        model=settings.voice_openai_asr_model,
        timeout=settings.voice_asr_timeout_seconds,
    )


def tts_provider_for(settings: ApiSettings) -> TTSProvider:
    if not settings.openai_api_key:
        raise VoiceConfigError(
            "OPENAI_API_KEY is not configured.",
            code="VOICE_PROVIDER_NOT_CONFIGURED",
        )
    return OpenAITTSProvider(
        api_key=settings.openai_api_key,
        model=settings.voice_openai_tts_model,
        voice=settings.voice_openai_tts_voice,
        timeout=settings.voice_tts_timeout_seconds,
    )


def asr_configured(settings: ApiSettings) -> bool:
    """Cheap boolean check -- safe to expose publicly (never the key itself).

    Used by ``GET``/``POST /widget/session`` to tell the widget whether to
    even attempt the cloud ASR path before it asks the visitor for
    microphone access.
    """
    return bool(settings.openai_api_key)


def tts_configured(settings: ApiSettings) -> bool:
    """Cheap boolean check -- see ``asr_configured``'s docstring."""
    return bool(settings.openai_api_key)
