"""Unit tests for OpenAICompatibleProvider.generate, embed, classify, and stream.

Uses a stub client to avoid real network calls. Verifies:
- Returns Completion with text + usage + finish_reason.
- Call kwargs contain ONLY model/max_tokens/messages (no temperature/top_p).
- content=None → text == "".
- Empty choices → LLMError.
- openai.APIError → LLMError (no fabricated text).
- embed returns vectors in order; kwargs are exactly {model, input}.
- Empty embeddings data → LLMError.
- embed sub-batches texts into groups of embedding_batch_size (S12.6): small
  input → 1 call; large input → N calls, order preserved; exact multiple →
  no trailing empty batch; empty input → 0 calls; mid-batch error → stops
  immediately, no further calls.
- classify returns canonical-cased label; non-label → LLMError; empty labels → LLMError.
- stream yields Chunk deltas in order; empty/None deltas skipped.
- Upstream-error logging emits WARNING with status/detail; api_key never logged.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APIError

from api.llm.openai_provider import OpenAICompatibleProvider
from api.llm.provider import ChatMessage, Chunk, Completion, LLMError


class _StubCompletions:
    """Stub for ``client.chat.completions`` with an async ``create`` method.

    When ``stream=True`` is passed, returns an async iterator of events instead
    of a single response.
    """

    def __init__(
        self,
        *,
        text: str = "Hello from the stub.",
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
        finish_reason: str = "stop",
        raise_error: Exception | None = None,
        empty_choices: bool = False,
        content_none: bool = False,
        stream_events: list[SimpleNamespace] | None = None,
    ) -> None:
        self._text = text
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._finish_reason = finish_reason
        self._raise_error = raise_error
        self._empty_choices = empty_choices
        self._content_none = content_none
        self._stream_events = stream_events
        self.last_kwargs: dict[str, object] = {}

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.last_kwargs = kwargs
        if self._raise_error is not None:
            raise self._raise_error
        if kwargs.get("stream"):
            return _AsyncIterator(self._stream_events or [])
        if self._empty_choices:
            return SimpleNamespace(choices=[], usage=None)
        message = SimpleNamespace(content=None if self._content_none else self._text)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=self._finish_reason)],
            usage=SimpleNamespace(
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
            ),
        )


class _AsyncIterator:
    """Async iterator wrapper around a list of events."""

    def __init__(self, events: list[SimpleNamespace]) -> None:
        self._events = events
        self._index = 0

    def __aiter__(self) -> _AsyncIterator:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event


class _StubEmbeddings:
    """Stub for ``client.embeddings`` with an async ``create`` method."""

    def __init__(
        self,
        *,
        vectors: list[list[float]] | None = None,
        raise_error: Exception | None = None,
        empty_data: bool = False,
    ) -> None:
        self._vectors = vectors or [[0.1, 0.2, 0.3]]
        self._raise_error = raise_error
        self._empty_data = empty_data
        self.last_kwargs: dict[str, object] = {}

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.last_kwargs = kwargs
        if self._raise_error is not None:
            raise self._raise_error
        if self._empty_data:
            return SimpleNamespace(data=[])
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=v) for v in self._vectors],
        )


class _BatchTrackingEmbeddings:
    """Stub for ``client.embeddings`` that records each call's ``input`` batch.

    Each returned vector encodes its own global index (``[0.0]``, ``[1.0]``,
    ...) so tests can assert order-preservation across sub-batched calls, not
    just call count.
    """

    def __init__(
        self,
        *,
        raise_on_call_index: int | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._raise_on_call_index = raise_on_call_index
        self._raise_error = raise_error
        self._global_index = 0

    async def create(self, **kwargs: object) -> SimpleNamespace:
        call_index = len(self.calls)
        batch = list(kwargs["input"])  # type: ignore[arg-type]
        self.calls.append(batch)

        if self._raise_on_call_index is not None and call_index == self._raise_on_call_index:
            raise self._raise_error if self._raise_error is not None else RuntimeError(
                "unexpected error"
            )

        data = []
        for _ in batch:
            data.append(SimpleNamespace(embedding=[float(self._global_index)]))
            self._global_index += 1
        return SimpleNamespace(data=data)


def _make_stub_client(
    completions: _StubCompletions | None = None,
    embeddings: _StubEmbeddings | None = None,
) -> MagicMock:
    """Build a mock client with optional completions and embeddings stubs."""
    client = MagicMock()
    if completions is not None:
        client.chat.completions = completions
    if embeddings is not None:
        client.embeddings = embeddings
    return client


async def test_generate_returns_completion_with_text_and_usage() -> None:
    """generate returns a Completion with the message text + usage counts."""
    stub = _StubCompletions(text="Hi there.", prompt_tokens=20, completion_tokens=8)
    client = _make_stub_client(stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.generate(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    )

    assert isinstance(result, Completion)
    assert result.text == "Hi there."
    assert result.model == "gpt-4o"
    assert result.input_tokens == 20
    assert result.output_tokens == 8
    assert result.stop_reason == "stop"


async def test_generate_does_not_send_forbidden_params() -> None:
    """The call kwargs must contain ONLY model, max_tokens, messages."""
    stub = _StubCompletions()
    client = _make_stub_client(stub)
    provider = OpenAICompatibleProvider(client=client)

    await provider.generate(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    )

    assert set(stub.last_kwargs.keys()) == {"model", "max_tokens", "messages"}
    for forbidden in ("temperature", "top_p", "top_k", "thinking"):
        assert forbidden not in stub.last_kwargs


async def test_generate_content_none_yields_empty_text() -> None:
    """When message.content is None, text should be empty string."""
    stub = _StubCompletions(content_none=True)
    client = _make_stub_client(stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.generate(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    )

    assert result.text == ""


async def test_generate_empty_choices_raises_llm_error() -> None:
    """Empty choices list → LLMError (no fabricated text)."""
    stub = _StubCompletions(empty_choices=True)
    client = _make_stub_client(stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.generate(
            [ChatMessage("user", "Hello")],
            model="gpt-4o",
            max_tokens=512,
        )


async def test_generate_wraps_api_error_in_llm_error() -> None:
    """An openai.APIError is wrapped in LLMError -- no fabricated text."""
    mock_request = MagicMock()
    api_err = APIError(
        message="upstream error",
        request=mock_request,
        body={"error": "fail"},
    )
    stub = _StubCompletions(raise_error=api_err)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.generate(
            [ChatMessage("user", "Hello")],
            model="gpt-4o",
            max_tokens=512,
        )


# -- embed tests ---------------------------------------------------------------


async def test_embed_returns_vectors_in_order() -> None:
    """embed returns the vectors in the same order as input texts."""
    stub = _StubEmbeddings(vectors=[[0.1, 0.2], [0.3, 0.4, 0.5]])
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.embed(
        ["hello", "world"],
        model="text-embedding-3-small",
    )

    assert result == [[0.1, 0.2], [0.3, 0.4, 0.5]]


async def test_embed_kwargs_are_exactly_model_and_input() -> None:
    """The call kwargs must contain ONLY model and input."""
    stub = _StubEmbeddings()
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client)

    await provider.embed(
        ["hello world"],
        model="nomic-embed-text",
    )

    assert set(stub.last_kwargs.keys()) == {"model", "input"}
    assert stub.last_kwargs["model"] == "nomic-embed-text"
    assert stub.last_kwargs["input"] == ["hello world"]


async def test_embed_empty_data_raises_llm_error() -> None:
    """Empty embeddings data → LLMError (no fabricated vector)."""
    stub = _StubEmbeddings(empty_data=True)
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.embed(
            ["hello"],
            model="nomic-embed-text",
        )


async def test_embed_wraps_api_error_in_llm_error() -> None:
    """An openai.APIError on embed is wrapped in LLMError."""
    mock_request = MagicMock()
    api_err = APIError(
        message="bad model",
        request=mock_request,
        body={"error": "invalid_model"},
    )
    stub = _StubEmbeddings(raise_error=api_err)
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.embed(
            ["hello"],
            model="nonexistent-model",
        )


# -- embed sub-batching tests (S12.6) -------------------------------------------


async def test_embed_small_input_uses_single_batch_call() -> None:
    """texts shorter than embedding_batch_size -> exactly one call, input unchanged."""
    stub = _BatchTrackingEmbeddings()
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client, embedding_batch_size=5)

    texts = ["a", "b", "c"]
    result = await provider.embed(texts, model="nomic-embed-text")

    assert len(stub.calls) == 1
    assert stub.calls[0] == texts
    assert result == [[0.0], [1.0], [2.0]]


async def test_embed_large_input_spans_multiple_batches_in_order() -> None:
    """texts longer than embedding_batch_size -> N calls, bounded size, order preserved."""
    stub = _BatchTrackingEmbeddings()
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client, embedding_batch_size=2)

    texts = ["t0", "t1", "t2", "t3", "t4"]
    result = await provider.embed(texts, model="nomic-embed-text")

    assert [len(c) for c in stub.calls] == [2, 2, 1]
    assert result == [[0.0], [1.0], [2.0], [3.0], [4.0]]


async def test_embed_exact_multiple_has_no_trailing_empty_batch() -> None:
    """texts length exactly divisible by embedding_batch_size -> no trailing empty call."""
    stub = _BatchTrackingEmbeddings()
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client, embedding_batch_size=2)

    texts = ["t0", "t1", "t2", "t3"]
    result = await provider.embed(texts, model="nomic-embed-text")

    assert len(stub.calls) == 2
    assert [len(c) for c in stub.calls] == [2, 2]
    assert result == [[0.0], [1.0], [2.0], [3.0]]


async def test_embed_empty_input_makes_no_calls() -> None:
    """texts=[] -> zero calls, returns []."""
    stub = _BatchTrackingEmbeddings()
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client, embedding_batch_size=5)

    result = await provider.embed([], model="nomic-embed-text")

    assert stub.calls == []
    assert result == []


async def test_embed_mid_batch_error_stops_immediately() -> None:
    """A failure on a non-first batch raises LLMError and issues no further calls."""
    mock_request = MagicMock()
    api_err = APIError(
        message="upstream failure",
        request=mock_request,
        body={"error": "fail"},
    )
    stub = _BatchTrackingEmbeddings(raise_on_call_index=1, raise_error=api_err)
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client, embedding_batch_size=2)

    texts = ["t0", "t1", "t2", "t3", "t4"]
    with pytest.raises(LLMError):
        await provider.embed(texts, model="nomic-embed-text")

    # Only the first (successful) batch and the second (failing) batch ran --
    # no batches after the failure (fail-fast, no silent partial result).
    assert len(stub.calls) == 2


async def test_embed_batching_defaults_to_single_call_when_unspecified() -> None:
    """Default embedding_batch_size (5) with a small input keeps prior single-call behavior."""
    stub = _StubEmbeddings(vectors=[[0.1, 0.2], [0.3, 0.4]])
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(client=client)  # no embedding_batch_size override

    result = await provider.embed(["hello", "world"], model="nomic-embed-text")

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert set(stub.last_kwargs.keys()) == {"model", "input"}
    assert stub.last_kwargs["input"] == ["hello", "world"]


# -- upstream-error logging tests ----------------------------------------------


async def test_generate_error_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """generate APIError → WARNING logged with status/detail; api_key never appears."""
    sentinel_key = "sk-SECRET-KEY-NEVER-LOG"
    mock_request = MagicMock()
    api_err = APIError(
        message="rate limited",
        request=mock_request,
        body={"error": "rate_limit"},
    )
    stub = _StubCompletions(raise_error=api_err)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.openai_provider"):
        with pytest.raises(LLMError):
            await provider.generate(
                [ChatMessage("user", "Hello")],
                model="gpt-4o",
                max_tokens=512,
            )

    assert any("rate limited" in record.message for record in caplog.records)
    assert any("generate" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


async def test_embed_error_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """embed APIError → WARNING logged with status/detail; api_key never appears."""
    sentinel_key = "sk-SECRET-KEY-NEVER-LOG"
    mock_request = MagicMock()
    api_err = APIError(
        message="bad model",
        request=mock_request,
        body={"error": "invalid_model"},
    )
    stub = _StubEmbeddings(raise_error=api_err)
    client = _make_stub_client(embeddings=stub)
    provider = OpenAICompatibleProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.openai_provider"):
        with pytest.raises(LLMError):
            await provider.embed(
                ["hello"],
                model="nonexistent",
            )

    assert any("bad model" in record.message for record in caplog.records)
    assert any("embed" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


# -- classify tests ------------------------------------------------------------


async def test_classify_returns_canonical_label() -> None:
    """classify returns the canonical-cased member of labels."""
    stub = _StubCompletions(text="Support")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="gpt-4o",
    )

    assert result == "Support"


async def test_classify_matches_case_insensitively() -> None:
    """classify matches case-insensitively and returns the canonical label."""
    stub = _StubCompletions(text="support")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.classify(
        "I need help",
        labels=["Sales", "Support", "Billing"],
        model="gpt-4o",
    )

    assert result == "Support"


async def test_classify_non_label_raises_llm_error() -> None:
    """classify with a reply not in labels → LLMError."""
    stub = _StubCompletions(text="UnknownCategory")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=["Sales", "Support", "Billing"],
            model="gpt-4o",
        )


async def test_classify_empty_labels_raises_llm_error() -> None:
    """classify with empty labels → LLMError, no client call."""
    tracking_client = MagicMock()
    client = MagicMock()
    client.chat.completions = tracking_client
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=[],
            model="gpt-4o",
        )

    tracking_client.assert_not_called()


async def test_classify_system_instruction_is_strict_non_conversational() -> None:
    """classify's system instruction forbids conversational replies (weak-model prompt-discipline fix)."""
    stub = _StubCompletions(text="Support")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="gpt-4o",
    )

    system_message = stub.last_kwargs["messages"][0]
    assert system_message["role"] == "system"
    instruction = system_message["content"]
    assert "classification" in instruction.lower()
    assert "not a conversational assistant" in instruction.lower()
    assert "do not" in instruction.lower() or "do not" in instruction.lower()
    assert "engage" in instruction.lower() or "respond to" in instruction.lower()
    assert "only the label" in instruction.lower() or "only one label" in instruction.lower()


async def test_classify_system_instruction_includes_balanced_fewshot_examples() -> None:
    """classify's system instruction includes few-shot examples spanning multiple distinct labels."""
    stub = _StubCompletions(text="Support")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="gpt-4o",
    )

    instruction = stub.last_kwargs["messages"][0]["content"]
    assert "example" in instruction.lower()
    # Examples must map to at least two DIFFERENT example labels (not all the same),
    # so the classifier isn't biased toward one label.
    example_lines = [line for line in instruction.splitlines() if line.lower().startswith("label:")]
    assert len(example_lines) >= 2
    assert len(set(example_lines)) >= 2


async def test_classify_passes_label_descriptions_into_the_instruction() -> None:
    """label_descriptions reaches the system message sent upstream -- proves
    the off-topic-misclassification fix is wired through to the real OpenAI
    call, not just built in classify_matching.py."""
    stub = _StubCompletions(text="off_topic")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    await provider.classify(
        "Who is the President of India?",
        labels=["question", "off_topic"],
        model="gpt-4o",
        label_descriptions={"off_topic": "unrelated to this business"},
    )

    instruction = stub.last_kwargs["messages"][0]["content"]
    assert "Label meanings" in instruction
    assert "off_topic: unrelated to this business" in instruction


async def test_classify_without_label_descriptions_omits_label_meanings() -> None:
    """Omitting label_descriptions (the default) keeps today's instruction
    byte-for-byte -- no behavior change for callers that don't opt in."""
    stub = _StubCompletions(text="Support")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    await provider.classify(
        "I need help with my order",
        labels=["Sales", "Support", "Billing"],
        model="gpt-4o",
    )

    assert "Label meanings" not in stub.last_kwargs["messages"][0]["content"]


async def test_classify_empty_choices_raises_llm_error() -> None:
    """classify with empty choices → LLMError."""
    stub = _StubCompletions(empty_choices=True)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=["Sales", "Support"],
            model="gpt-4o",
        )


async def test_classify_wraps_api_error_in_llm_error() -> None:
    """classify APIError → LLMError."""
    mock_request = MagicMock()
    api_err = APIError(
        message="rate limited",
        request=mock_request,
        body={"error": "rate_limit"},
    )
    stub = _StubCompletions(raise_error=api_err)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=["Sales", "Support"],
            model="gpt-4o",
        )


async def test_classify_error_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """classify APIError → WARNING logged; api_key never appears."""
    sentinel_key = "sk-SECRET-KEY-NEVER-LOG"
    mock_request = MagicMock()
    api_err = APIError(
        message="rate limited",
        request=mock_request,
        body={"error": "rate_limit"},
    )
    stub = _StubCompletions(raise_error=api_err)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.openai_provider"):
        with pytest.raises(LLMError):
            await provider.classify(
                "I need help",
                labels=["Sales", "Support"],
                model="gpt-4o",
            )

    assert any("classify" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


async def test_classify_no_match_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """classify no-match → WARNING logged with reply substring; api_key never appears."""
    sentinel_key = "sk-SECRET-KEY-NEVER-LOG"
    stub = _StubCompletions(text="I am not sure what category this is.")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.openai_provider"):
        with pytest.raises(LLMError):
            await provider.classify(
                "I need help",
                labels=["sales", "support", "billing"],
                model="gpt-4o",
            )

    assert any("no matching label" in record.message for record in caplog.records)
    assert any("not sure what category" in record.message for record in caplog.records)
    assert all(sentinel_key not in record.message for record in caplog.records)


async def test_classify_trailing_punctuation_matches_label() -> None:
    """classify strips trailing punctuation/whitespace before matching."""
    stub = _StubCompletions(text="support. \n")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.classify(
        "I need help",
        labels=["sales", "support", "billing"],
        model="gpt-4o",
    )

    assert result == "support"


async def test_classify_trailing_plural_matches_label() -> None:
    """classify depluralizes a naive trailing 's' -- the live-bug scenario."""
    stub = _StubCompletions(text="scheduling_requests")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.classify(
        "book me a call",
        labels=["question", "chitchat", "scheduling_request", "off_topic", "other"],
        model="gpt-4o",
    )

    assert result == "scheduling_request"


async def test_classify_whole_word_substring_matches_label() -> None:
    """classify recognizes a label wrapped in a sentence via whole-word match."""
    stub = _StubCompletions(text="The correct label is scheduling_request.")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    result = await provider.classify(
        "book me a call",
        labels=["question", "chitchat", "scheduling_request", "off_topic", "other"],
        model="gpt-4o",
    )

    assert result == "scheduling_request"


async def test_classify_ambiguous_substring_raises_llm_error() -> None:
    """classify does not guess when two labels both loosely match -- fail loud."""
    stub = _StubCompletions(text="This could be sales or support, not sure which.")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        await provider.classify(
            "I need help",
            labels=["sales", "support", "billing"],
            model="gpt-4o",
        )


async def test_classify_fallback_match_logs_info_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-exact (fallback) match logs at INFO, not WARNING."""
    stub = _StubCompletions(text="scheduling_requests")
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with caplog.at_level(logging.INFO, logger="api.llm.openai_provider"):
        result = await provider.classify(
            "book me a call",
            labels=["question", "chitchat", "scheduling_request", "off_topic", "other"],
            model="gpt-4o",
        )

    assert result == "scheduling_request"
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert any(record.levelno == logging.INFO for record in caplog.records)


async def test_classify_no_match_truncates_reply(caplog: pytest.LogCaptureFixture) -> None:
    """classify no-match → logged reply is truncated to 120 chars, not the full reply."""
    long_reply = "x" * 200
    stub = _StubCompletions(text=long_reply)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.openai_provider"):
        with pytest.raises(LLMError):
            await provider.classify(
                "I need help",
                labels=["sales", "support", "billing"],
                model="gpt-4o",
            )

    assert all("x" * 200 not in record.message for record in caplog.records)
    assert any("x" * 120 in record.message for record in caplog.records)


# -- stream tests --------------------------------------------------------------


async def test_stream_yields_chunk_deltas_in_order() -> None:
    """stream yields Chunk(text=...) for each delta.content."""
    stream_events = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="He"))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="llo"))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" world"))]),
    ]
    stub = _StubCompletions(stream_events=stream_events)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    chunks: list[Chunk] = []
    async for chunk in provider.stream(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    ):
        chunks.append(chunk)

    assert chunks == [Chunk("He"), Chunk("llo"), Chunk(" world")]


async def test_stream_skips_none_and_empty_deltas() -> None:
    """stream skips events where delta.content is None or missing."""
    stream_events = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hi"))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace())]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="!"))]),
    ]
    stub = _StubCompletions(stream_events=stream_events)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    chunks: list[Chunk] = []
    async for chunk in provider.stream(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    ):
        chunks.append(chunk)

    assert chunks == [Chunk("Hi"), Chunk("!")]


async def test_stream_wraps_api_error_in_llm_error() -> None:
    """stream APIError on open → LLMError."""
    mock_request = MagicMock()
    api_err = APIError(
        message="bad model",
        request=mock_request,
        body={"error": "invalid_model"},
    )
    stub = _StubCompletions(raise_error=api_err)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(client=client)

    with pytest.raises(LLMError):
        async for _chunk in provider.stream(
            [ChatMessage("user", "Hello")],
            model="bad-model",
            max_tokens=512,
        ):
            pass


async def test_stream_error_logs_warning_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """stream APIError → WARNING logged; api_key never appears."""
    sentinel_key = "sk-SECRET-KEY-NEVER-LOG"
    mock_request = MagicMock()
    api_err = APIError(
        message="bad model",
        request=mock_request,
        body={"error": "invalid_model"},
    )
    stub = _StubCompletions(raise_error=api_err)
    client = _make_stub_client(completions=stub)
    provider = OpenAICompatibleProvider(api_key=sentinel_key, client=client)

    with caplog.at_level(logging.WARNING, logger="api.llm.openai_provider"):
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


async def test_openai_forwards_max_retries_and_timeout() -> None:
    """When no client= is injected, AsyncOpenAI is built with max_retries + timeout."""
    with patch("openai.AsyncOpenAI") as MockOpenAI:
        MockOpenAI.return_value = MagicMock()
        OpenAICompatibleProvider(
            api_key="sk-test-key",
            base_url="https://example.com/v1",
            max_retries=5,
            timeout=45.0,
        )
        MockOpenAI.assert_called_once_with(
            api_key="sk-test-key",
            base_url="https://example.com/v1",
            max_retries=5,
            timeout=45.0,
        )


# -- aclose ----------------------------------------------------------------


async def test_aclose_calls_underlying_client_close() -> None:
    """aclose() awaits the injected client's close() exactly once (resource-leak fix).

    Verified against the installed SDK: ``AsyncOpenAI`` exposes ``async def
    close(self) -> None`` at ``openai/_base_client.py`` (``AsyncAPIClient.close``).
    """
    client = MagicMock()
    client.close = AsyncMock()
    provider = OpenAICompatibleProvider(client=client)

    await provider.aclose()

    client.close.assert_awaited_once_with()
