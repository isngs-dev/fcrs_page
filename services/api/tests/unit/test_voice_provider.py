"""Unit tests for the voice (ASR/TTS) provider boundary.

Covers:
- asr_provider_for / tts_provider_for raise VoiceConfigError when
  OPENAI_API_KEY is unset -- never construct a provider against a missing
  key. Both directions share the same key (one OpenAI account).
- asr_configured / tts_configured booleans reflect settings truthfully.
- OpenAIASRProvider.transcribe: happy path (stub client) + openai.APIError ->
  VoiceProviderError (no fabricated transcript).
- OpenAITTSProvider.synthesize: happy path (stub client) + openai.APIError ->
  VoiceProviderError (no fabricated audio).
- truncate_for_speech: the per-message TTS cost ceiling.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import APIError

from api.voice.factory import asr_configured, asr_provider_for, tts_configured, tts_provider_for
from api.voice.openai_asr_provider import OpenAIASRProvider
from api.voice.openai_tts_provider import OpenAITTSProvider
from api.voice.provider import VoiceConfigError, VoiceProviderError, truncate_for_speech


def _settings(**overrides: object) -> object:
    base = {
        "openai_api_key": None,
        "voice_openai_asr_model": "whisper-1",
        "voice_openai_tts_model": "tts-1",
        "voice_openai_tts_voice": "nova",
        "voice_asr_timeout_seconds": 20.0,
        "voice_tts_timeout_seconds": 20.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ==============================================================================
# Factory config errors / configured() booleans
# ==============================================================================


def test_asr_provider_for_raises_config_error_when_key_unset() -> None:
    with pytest.raises(VoiceConfigError):
        asr_provider_for(_settings())


def test_tts_provider_for_raises_config_error_when_key_unset() -> None:
    with pytest.raises(VoiceConfigError):
        tts_provider_for(_settings())


def test_asr_provider_for_returns_provider_when_configured() -> None:
    provider = asr_provider_for(_settings(openai_api_key="sk-fake"))
    assert isinstance(provider, OpenAIASRProvider)


def test_tts_provider_for_returns_provider_when_configured() -> None:
    provider = tts_provider_for(_settings(openai_api_key="sk-fake"))
    assert isinstance(provider, OpenAITTSProvider)


def test_asr_configured_reflects_settings() -> None:
    assert asr_configured(_settings()) is False
    assert asr_configured(_settings(openai_api_key="sk-fake")) is True


def test_tts_configured_reflects_settings() -> None:
    assert tts_configured(_settings()) is False
    assert tts_configured(_settings(openai_api_key="sk-fake")) is True


def test_asr_and_tts_configured_track_the_same_single_key() -> None:
    """One OpenAI account covers both directions -- there is no way to
    configure one without the other, unlike the old ElevenLabs split."""
    settings = _settings(openai_api_key="sk-fake")
    assert asr_configured(settings) == tts_configured(settings) is True


# ==============================================================================
# OpenAIASRProvider
# ==============================================================================


class _StubTranscriptions:
    def __init__(self, *, text: str = "hello there", raise_error: Exception | None = None) -> None:
        self._text = text
        self._raise_error = raise_error
        self.last_kwargs: dict[str, object] = {}

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.last_kwargs = kwargs
        if self._raise_error is not None:
            raise self._raise_error
        return SimpleNamespace(text=self._text)


def _make_stub_asr_client(transcriptions: _StubTranscriptions) -> MagicMock:
    client = MagicMock()
    client.audio = SimpleNamespace(transcriptions=transcriptions)
    return client


async def test_transcribe_returns_stripped_text() -> None:
    stub = _StubTranscriptions(text="  hello there  ")
    client = _make_stub_asr_client(stub)
    provider = OpenAIASRProvider(model="whisper-1", client=client)

    result = await provider.transcribe(b"fake-audio-bytes", content_type="audio/webm")

    assert result == "hello there"
    assert stub.last_kwargs["model"] == "whisper-1"
    filename, audio_bytes, content_type = stub.last_kwargs["file"]  # type: ignore[misc]
    assert filename == "audio.webm"
    assert audio_bytes == b"fake-audio-bytes"
    assert content_type == "audio/webm"


async def test_transcribe_wraps_api_error_in_voice_provider_error() -> None:
    mock_request = MagicMock()
    api_err = APIError(message="upstream error", request=mock_request, body={"error": "fail"})
    stub = _StubTranscriptions(raise_error=api_err)
    client = _make_stub_asr_client(stub)
    provider = OpenAIASRProvider(model="whisper-1", client=client)

    with pytest.raises(VoiceProviderError):
        await provider.transcribe(b"fake-audio-bytes", content_type="audio/webm")


async def test_transcribe_derives_filename_extension_from_content_type() -> None:
    stub = _StubTranscriptions()
    client = _make_stub_asr_client(stub)
    provider = OpenAIASRProvider(model="whisper-1", client=client)

    await provider.transcribe(b"x", content_type="audio/mp4;codecs=mp4a.40.2")

    filename = stub.last_kwargs["file"][0]  # type: ignore[index]
    assert filename == "audio.mp4"


# ==============================================================================
# OpenAITTSProvider
# ==============================================================================


class _StubSpeech:
    def __init__(self, *, audio: bytes = b"fake-mp3-bytes", raise_error: Exception | None = None) -> None:
        self._audio = audio
        self._raise_error = raise_error
        self.last_kwargs: dict[str, object] = {}

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.last_kwargs = kwargs
        if self._raise_error is not None:
            raise self._raise_error
        return SimpleNamespace(content=self._audio)


def _make_stub_tts_client(speech: _StubSpeech) -> MagicMock:
    client = MagicMock()
    client.audio = SimpleNamespace(speech=speech)
    return client


async def test_synthesize_returns_audio_bytes_on_success() -> None:
    stub = _StubSpeech(audio=b"fake-mp3-bytes")
    client = _make_stub_tts_client(stub)
    provider = OpenAITTSProvider(model="tts-1", voice="nova", client=client)

    result = await provider.synthesize("Hello there")

    assert result == b"fake-mp3-bytes"
    assert stub.last_kwargs == {"model": "tts-1", "voice": "nova", "input": "Hello there"}


async def test_synthesize_wraps_api_error_in_voice_provider_error() -> None:
    mock_request = MagicMock()
    api_err = APIError(message="upstream error", request=mock_request, body={"error": "fail"})
    stub = _StubSpeech(raise_error=api_err)
    client = _make_stub_tts_client(stub)
    provider = OpenAITTSProvider(model="tts-1", voice="nova", client=client)

    with pytest.raises(VoiceProviderError):
        await provider.synthesize("Hello there")


# ==============================================================================
# truncate_for_speech -- the per-message TTS cost ceiling
# ==============================================================================


def test_truncate_for_speech_returns_unchanged_text_under_the_limit() -> None:
    text = "We're open Monday through Friday, 9 to 6."
    assert truncate_for_speech(text, 1200) == text


def test_truncate_for_speech_cuts_at_the_last_sentence_boundary_at_or_before_the_limit() -> None:
    text = "First sentence here. Second sentence is a bit longer than the first. Third one."
    result = truncate_for_speech(text, 50)
    assert result == "First sentence here."
    assert len(result) <= 50


def test_truncate_for_speech_falls_back_to_a_word_boundary_with_no_sentence_ending_in_range() -> None:
    text = "one two three four five six seven eight nine ten eleven twelve"
    result = truncate_for_speech(text, 20)
    assert result == "one two three four"
    assert len(result) <= 20
    assert not result.endswith(" ")


def test_truncate_for_speech_hard_cuts_when_no_boundary_exists_at_all() -> None:
    text = "a" * 40
    result = truncate_for_speech(text, 10)
    assert result == "a" * 10


def test_truncate_for_speech_never_returns_longer_than_max_chars() -> None:
    text = "Sentence one. " * 200
    result = truncate_for_speech(text, 1200)
    assert len(result) <= 1200
