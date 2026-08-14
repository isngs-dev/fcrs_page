"""Unit tests for AnthropicProvider.embed, classify, stream, and upstream-error logging.

Covers:
- AnthropicProvider.embed raises LLMError (no network call, no fabricated vector).
- generate APIError → WARNING logged with status/detail; api_key never appears.
- classify returns canonical label; non-label → LLMError; empty labels → LLMError.
- stream yields Chunk deltas from content_block_delta/text_delta events.
- stream APIError → LLMError + WARNING without sentinel key.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic import APIError

from api.llm.anthropic_provider import AnthropicProvider
from api.llm.provider import ChatMessage, Chunk, LLMError


class _StubMessages:
    """Stub for ``client.messages`` with an async ``create`` method.

    When ``stream=True`` is passed, returns an async iterator of events.
    """

    def __init__(
        self,
        *,
        text: str = "Hello from the stub.",
        input_tokens: int = 10,
        output_tokens: int = 5,
        stop_reason: str = "end_turn",
        raise_error: Exception | None = None,
        stream_events: list | None = None,
    ) -> None:
        self._text = text
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._stop_reason = stop_reason
        self._raise_error = raise_error
        self._stream_events = stream_events
        self.last_kwargs: dict[str, object] = {}

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.last_kwargs = kwargs
        if self._raise_error is not None:
            raise self._raise_error
        if kwargs.get("stream"):
            return _AsyncIterator(self._stream_events or [])
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            usage=SimpleNamespace(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
            stop_reason=self._stop_reason,
        )


class _AsyncIterator:
    """Async iterator wrapper around a list of events."""

    def __init__(self, events: list) -> None:
        self._events = events
        self._index = 0

    def __aiter__(self) -> _AsyncIterator:
        return self

    async def __anext__(self) -> object:
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event


def _make_stub_client(messages: _StubMessages) -> MagicMock:
    """Build a mock client whose ``messages`` attribute is the stub."""
    client = MagicMock()
    client.messages = messages
    return client


# -- embed raises LLMError on Anthropic ----------------------------------------


async def test_embed_raises_llm_error_on_anthropic() -> None:
    """Anthropic has no embeddings API → LLMError, no client call."""
    tracking_client = MagicMock()
    provider = AnthropicProvider(client=tracking_client)

    with pytest.raises(LLMError) as exc_info:
        await provider.embed(
            ["hello world"],
            model="some-embed-model",
        )

    assert "Embeddings are not supported" in str(exc_info.value)
    # No method on the client should have been called
    tracking_client.assert_not_called()


# -- upstream-error logging for generate ---------------------------------------


async def test_generate_error_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """generate APIError → WARNING logged with status/detail; api_key never appears."""
    sentinel_key = "sk-ant-SECRET-KEY-NEVER-LOG"
    mock_request = MagicMock()
    api_err = APIError("upstream error", request=mock_request, body={"error": "fail"})
    stub = _StubMessages(raise_error=api_err)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.anthropic_provider"):
        with pytest.raises(LLMError):
            await provider.generate(
                [ChatMessage("user", "Hello")],
                model="claude-opus-4-8",
                max_tokens=512,
            )

    assert any("upstream error" in record.message for record in caplog.records)
    assert any("generate" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


# -- classify tests ------------------------------------------------------------


async def test_classify_returns_canonical_label() -> None:
    """classify returns the canonical-cased member of labels."""
    stub = _StubMessages(text="Support")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    result = await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="claude-opus-4-8",
    )

    assert result == "Support"


async def test_classify_matches_case_insensitively() -> None:
    """classify matches case-insensitively and returns the canonical label."""
    stub = _StubMessages(text="support")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    result = await provider.classify(
        "I need help",
        labels=["Sales", "Support", "Billing"],
        model="claude-opus-4-8",
    )

    assert result == "Support"


async def test_classify_non_label_raises_llm_error() -> None:
    """classify with a reply not in labels → LLMError."""
    stub = _StubMessages(text="UnknownCategory")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=["Sales", "Support", "Billing"],
            model="claude-opus-4-8",
        )


async def test_classify_empty_labels_raises_llm_error() -> None:
    """classify with empty labels → LLMError, no client call."""
    tracking_client = MagicMock()
    client = MagicMock()
    client.messages = tracking_client
    provider = AnthropicProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=[],
            model="claude-opus-4-8",
        )

    tracking_client.assert_not_called()


async def test_classify_system_instruction_is_strict_non_conversational() -> None:
    """classify's system instruction forbids conversational replies (weak-model prompt-discipline fix)."""
    stub = _StubMessages(text="Support")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="claude-opus-4-8",
    )

    instruction = stub.last_kwargs["system"]
    assert "classification" in instruction.lower()
    assert "not a conversational assistant" in instruction.lower()
    assert "engage" in instruction.lower() or "respond to" in instruction.lower()
    assert "only the label" in instruction.lower() or "only one label" in instruction.lower()


async def test_classify_system_instruction_includes_balanced_fewshot_examples() -> None:
    """classify's system instruction includes few-shot examples spanning multiple distinct labels."""
    stub = _StubMessages(text="Support")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="claude-opus-4-8",
    )

    instruction = stub.last_kwargs["system"]
    assert "example" in instruction.lower()
    example_lines = [line for line in instruction.splitlines() if line.lower().startswith("label:")]
    assert len(example_lines) >= 2
    assert len(set(example_lines)) >= 2


async def test_classify_passes_label_descriptions_into_the_instruction() -> None:
    """label_descriptions reaches the system instruction sent upstream --
    proves the off-topic-misclassification fix is actually wired through
    to the real Anthropic call, not just built in classify_matching.py."""
    stub = _StubMessages(text="off_topic")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    await provider.classify(
        "Who is the President of India?",
        labels=["question", "off_topic"],
        model="claude-opus-4-8",
        label_descriptions={"off_topic": "unrelated to this business"},
    )

    instruction = stub.last_kwargs["system"]
    assert "Label meanings" in instruction
    assert "off_topic: unrelated to this business" in instruction


async def test_classify_without_label_descriptions_omits_label_meanings() -> None:
    """Omitting label_descriptions (the default) keeps today's instruction
    byte-for-byte -- no behavior change for callers that don't opt in."""
    stub = _StubMessages(text="Support")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="claude-opus-4-8",
    )

    assert "Label meanings" not in stub.last_kwargs["system"]


async def test_classify_wraps_api_error_in_llm_error() -> None:
    """classify APIError → LLMError."""
    mock_request = MagicMock()
    api_err = APIError("rate limited", request=mock_request, body={"error": "rate_limit"})
    stub = _StubMessages(raise_error=api_err)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=["Sales", "Support"],
            model="claude-opus-4-8",
        )


async def test_classify_error_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """classify APIError → WARNING logged; api_key never appears."""
    sentinel_key = "sk-ant-SECRET-KEY-NEVER-LOG"
    mock_request = MagicMock()
    api_err = APIError("rate limited", request=mock_request, body={"error": "rate_limit"})
    stub = _StubMessages(raise_error=api_err)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.anthropic_provider"):
        with pytest.raises(LLMError):
            await provider.classify(
                "I need help",
                labels=["Sales", "Support"],
                model="claude-opus-4-8",
            )

    assert any("classify" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


async def test_classify_no_match_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """classify no-match → WARNING logged with reply substring; api_key never appears."""
    # Sentinel avoids the sk-ant-<20+> pattern checked by the secret-scan hook;
    # the behavioral assertion (key never appears in logs) is identical.
    sentinel_key = "ANT-SENTINEL-NEVER-LOG-NOMATCHTST"
    stub = _StubMessages(text="I am not sure what category this is.")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.anthropic_provider"):
        with pytest.raises(LLMError):
            await provider.classify(
                "I need help",
                labels=["sales", "support", "billing"],
                model="claude-opus-4-8",
            )

    assert any("no matching label" in record.message for record in caplog.records)
    assert any("not sure what category" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


async def test_classify_trailing_punctuation_matches_label() -> None:
    """classify strips trailing punctuation/whitespace before matching."""
    stub = _StubMessages(text="support. \n")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    result = await provider.classify(
        "I need help",
        labels=["sales", "support", "billing"],
        model="claude-opus-4-8",
    )

    assert result == "support"


async def test_classify_trailing_plural_matches_label() -> None:
    """classify depluralizes a naive trailing 's' -- the live-bug scenario."""
    stub = _StubMessages(text="scheduling_requests")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    result = await provider.classify(
        "book me a call",
        labels=["question", "chitchat", "scheduling_request", "off_topic", "other"],
        model="claude-opus-4-8",
    )

    assert result == "scheduling_request"


async def test_classify_whole_word_substring_matches_label() -> None:
    """classify recognizes a label wrapped in a sentence via whole-word match."""
    stub = _StubMessages(text="The correct label is scheduling_request.")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    result = await provider.classify(
        "book me a call",
        labels=["question", "chitchat", "scheduling_request", "off_topic", "other"],
        model="claude-opus-4-8",
    )

    assert result == "scheduling_request"


async def test_classify_ambiguous_substring_raises_llm_error() -> None:
    """classify does not guess when two labels both loosely match -- fail loud."""
    stub = _StubMessages(text="This could be sales or support, not sure which.")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=["sales", "support", "billing"],
            model="claude-opus-4-8",
        )


async def test_classify_fallback_match_logs_info_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-exact (fallback) match logs at INFO, not WARNING."""
    stub = _StubMessages(text="scheduling_requests")
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    with caplog.at_level(logging.INFO, logger="api.llm.anthropic_provider"):
        result = await provider.classify(
            "book me a call",
            labels=["question", "chitchat", "scheduling_request", "off_topic", "other"],
            model="claude-opus-4-8",
        )

    assert result == "scheduling_request"
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert any(record.levelno == logging.INFO for record in caplog.records)


async def test_classify_no_match_truncates_reply(caplog: pytest.LogCaptureFixture) -> None:
    """classify no-match → logged reply is truncated to 120 chars, not the full reply."""
    long_reply = "x" * 200
    stub = _StubMessages(text=long_reply)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.anthropic_provider"):
        with pytest.raises(LLMError):
            await provider.classify(
                "I need help",
                labels=["sales", "support", "billing"],
                model="claude-opus-4-8",
            )

    assert all("x" * 200 not in record.message for record in caplog.records)
    assert any("x" * 120 in record.message for record in caplog.records)


# -- stream tests --------------------------------------------------------------


async def test_stream_yields_chunk_deltas_in_order() -> None:
    """stream yields Chunk(text=...) for content_block_delta/text_delta events."""
    stream_events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="He"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="llo"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=" world"),
        ),
    ]
    stub = _StubMessages(stream_events=stream_events)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    chunks: list[Chunk] = []
    async for chunk in provider.stream(
        [ChatMessage("user", "Hello")],
        model="claude-opus-4-8",
        max_tokens=512,
    ):
        chunks.append(chunk)

    assert chunks == [Chunk("He"), Chunk("llo"), Chunk(" world")]


async def test_stream_skips_non_text_events() -> None:
    """stream skips events that are not content_block_delta/text_delta."""
    stream_events = [
        SimpleNamespace(
            type="content_block_start",
            delta=SimpleNamespace(type="text_delta", text="ignored"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Hi"),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(type="text_delta", text="ignored"),
        ),
    ]
    stub = _StubMessages(stream_events=stream_events)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    chunks: list[Chunk] = []
    async for chunk in provider.stream(
        [ChatMessage("user", "Hello")],
        model="claude-opus-4-8",
        max_tokens=512,
    ):
        chunks.append(chunk)

    assert chunks == [Chunk("Hi")]


async def test_stream_wraps_api_error_in_llm_error() -> None:
    """stream APIError on open → LLMError."""
    mock_request = MagicMock()
    api_err = APIError("bad model", request=mock_request, body={"error": "invalid_model"})
    stub = _StubMessages(raise_error=api_err)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(client=client)

    with pytest.raises(LLMError):
        async for _chunk in provider.stream(
            [ChatMessage("user", "Hello")],
            model="bad-model",
            max_tokens=512,
        ):
            pass


async def test_stream_error_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """stream APIError → WARNING logged; api_key never appears."""
    sentinel_key = "sk-ant-SECRET-KEY-NEVER-LOG"
    mock_request = MagicMock()
    api_err = APIError("bad model", request=mock_request, body={"error": "invalid_model"})
    stub = _StubMessages(raise_error=api_err)
    client = _make_stub_client(stub)
    provider = AnthropicProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.anthropic_provider"):
        with pytest.raises(LLMError):
            async for _chunk in provider.stream(
                [ChatMessage("user", "Hello")],
                model="bad-model",
                max_tokens=512,
            ):
                pass

    assert any("stream" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


# -- SDK construction: max_retries + timeout -----------------------------------


async def test_anthropic_forwards_max_retries_and_timeout() -> None:
    """When no client= is injected, AsyncAnthropic is built with max_retries + timeout."""
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        MockAnthropic.return_value = MagicMock()
        AnthropicProvider(
            api_key="sk-ant-key",
            max_retries=3,
            timeout=20.0,
        )
        MockAnthropic.assert_called_once_with(
            api_key="sk-ant-key",
            max_retries=3,
            timeout=20.0,
        )


# -- aclose ------------------------------------------------------------------


async def test_aclose_calls_underlying_client_close() -> None:
    """aclose() awaits the injected client's close() exactly once (resource-leak fix).

    Verified against the installed SDK: ``AsyncAnthropic`` overrides ``async
    def close(self) -> None`` at ``anthropic/_client.py`` (calling
    ``super().close()``, i.e. ``AsyncAPIClient.close`` in
    ``anthropic/_base_client.py``).
    """
    client = MagicMock()
    client.close = AsyncMock()
    provider = AnthropicProvider(client=client)

    await provider.aclose()

    client.close.assert_awaited_once_with()
