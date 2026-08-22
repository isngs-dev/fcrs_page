"""Voice (ASR/TTS) routes -- POST /public/chat/transcribe, POST /public/chat/speak.

Same visitor-authenticated (``get_visitor_claims``), leak-free shape as
``api.orchestrator.routes``. Config errors (``VOICE_PROVIDER_NOT_CONFIGURED``,
422) and upstream failures (``VOICE_PROVIDER_ERROR``, 502) propagate to the
centralized error middleware -- the widget is responsible for degrading to
its own browser-native mechanism on either, this route never guesses or
substitutes a fallback itself.
"""
from __future__ import annotations

from common.auth import AuthClaims
from common.errors import ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, field_validator

from api.config import get_api_settings
from api.gateway.dependencies import get_visitor_claims
from api.voice.factory import asr_provider_for, tts_provider_for
from api.voice.provider import truncate_for_speech

_log = get_logger(__name__)

router = APIRouter(prefix="/public/chat", tags=["voice"])


class TranscribeResponse(BaseModel):
    text: str


class SpeakRequest(BaseModel):
    """Body for POST /public/chat/speak."""

    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text must not be blank")
        return v


@router.post("/transcribe")
async def post_transcribe(
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> TranscribeResponse:
    """Transcribe one recorded utterance.

    Raw audio bytes as the request body (``Content-Type`` is the audio MIME
    type, e.g. ``audio/webm``) -- no multipart wrapper, matching the widget's
    ``MediaRecorder`` ``Blob`` upload directly.
    """
    settings = get_api_settings()
    audio = await request.body()
    if len(audio) > settings.voice_max_audio_upload_bytes:
        raise ValidationError(
            f"Audio exceeds the {settings.voice_max_audio_upload_bytes}-byte limit.",
            code="AUDIO_TOO_LARGE",
        )
    if not audio:
        raise ValidationError("Audio must not be empty.", code="AUDIO_EMPTY")

    content_type = request.headers.get("content-type") or "audio/webm"
    provider = asr_provider_for(settings)
    try:
        text = await provider.transcribe(audio, content_type=content_type)
    finally:
        await provider.aclose()

    _log.info(
        "voice transcribe",
        extra={
            "event": "voice_transcribe",
            "tenant_id": claims.tenant_id,
            "audio_bytes": len(audio),
        },
    )
    return TranscribeResponse(text=text)


@router.post("/speak")
async def post_speak(
    body: SpeakRequest,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> Response:
    """Synthesize speech for the given text.

    Returns raw audio bytes (``audio/mpeg``) -- the widget plays them
    directly via an ``<audio>`` element, no JSON wrapper.
    """
    settings = get_api_settings()
    provider = tts_provider_for(settings)
    text = truncate_for_speech(body.text, settings.voice_max_tts_chars)
    audio = await provider.synthesize(text)

    _log.info(
        "voice speak",
        extra={
            "event": "voice_speak",
            "tenant_id": claims.tenant_id,
            "text_chars": len(text),
            "truncated": len(text) < len(body.text),
        },
    )
    return Response(content=audio, media_type="audio/mpeg")
