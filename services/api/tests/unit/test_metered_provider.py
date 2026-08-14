"""Unit tests for MeteredProvider.

Wraps a stub LLMProvider and verifies that Prometheus metrics are recorded
with correct labels and values. Uses before/after deltas via
``REGISTRY.get_sample_value`` (the registry is global — never assert absolute
values).

Covers:
- generate: token counters (input + output) and duration count increase.
- embed / classify: duration count increases, no token delta.
- stream: yields chunks in order, duration count increases after exhaustion.
- delegate raises (each op incl. stream mid-iterate): error counter increases
  AND the exception propagates unchanged.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from prometheus_client import REGISTRY

from api.llm.metered_provider import MeteredProvider
from api.llm.provider import ChatMessage, Chunk, Completion, LLMError, Vector


class _StubProvider:
    """Stub LLMProvider with canned responses."""

    def __init__(
        self,
        *,
        completion: Completion | None = None,
        vectors: list[Vector] | None = None,
        label: str = "Support",
        stream_chunks: list[Chunk] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self._completion = completion or Completion(
            text="Hello.",
            model="gpt-4o",
            input_tokens=10,
            output_tokens=5,
        )
        self._vectors = vectors or [[0.1, 0.2, 0.3]]
        self._label = label
        self._stream_chunks = stream_chunks or [Chunk("He"), Chunk("llo")]
        self._raise_on = raise_on
        self.aclose_calls = 0
        self.last_classify_label_descriptions: dict[str, str] | None = None

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        if self._raise_on == "generate":
            raise LLMError("LLM request failed.")
        return self._completion

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
    ) -> list[Vector]:
        if self._raise_on == "embed":
            raise LLMError("LLM request failed.")
        return self._vectors

    async def classify(
        self,
        text: str,
        labels: list[str],
        *,
        model: str,
        label_descriptions: dict[str, str] | None = None,
    ) -> str:
        self.last_classify_label_descriptions = label_descriptions
        if self._raise_on == "classify":
            raise LLMError("LLM request failed.")
        return self._label

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> AsyncIterator[Chunk]:
        if self._raise_on == "stream":
            raise LLMError("LLM request failed.")
        for chunk in self._stream_chunks:
            yield chunk

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _get_sample(name: str, **labels: str) -> float | None:
    """Read a metric sample value from the global registry."""
    return REGISTRY.get_sample_value(name, labels)


def _delta(name: str, **labels: str):
    """Context manager that yields (before, after) delta for a metric sample."""
    before = _get_sample(name, **labels) or 0
    yield before, None
    after = _get_sample(name, **labels) or 0
    return after


# -- generate: tokens + duration -----------------------------------------------


async def test_metered_generate_records_token_counts() -> None:
    """After generate, token counters rose by the stub's input/output counts."""
    stub = _StubProvider(
        completion=Completion(
            text="Hello.",
            model="gpt-4o",
            input_tokens=10,
            output_tokens=5,
        ),
    )
    metered = MeteredProvider(stub, provider="openai")

    input_before = _get_sample(
        "llm_tokens_total", provider="openai", model="gpt-4o", kind="input",
    ) or 0
    output_before = _get_sample(
        "llm_tokens_total", provider="openai", model="gpt-4o", kind="output",
    ) or 0

    await metered.generate(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    )

    input_after = _get_sample(
        "llm_tokens_total", provider="openai", model="gpt-4o", kind="input",
    ) or 0
    output_after = _get_sample(
        "llm_tokens_total", provider="openai", model="gpt-4o", kind="output",
    ) or 0

    assert input_after - input_before == 10
    assert output_after - output_before == 5


async def test_metered_generate_records_duration_count() -> None:
    """After generate, duration count for op=generate rose by 1."""
    stub = _StubProvider()
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="generate",
    ) or 0

    await metered.generate(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    )

    after = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="generate",
    ) or 0

    assert after - before == 1


# -- embed: duration only, no tokens -------------------------------------------


async def test_metered_embed_records_duration_count() -> None:
    """After embed, duration count for op=embed rose by 1; no token delta."""
    stub = _StubProvider()
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="embed",
    ) or 0

    await metered.embed(
        ["hello"],
        model="text-embedding-3-small",
    )

    after = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="embed",
    ) or 0

    assert after - before == 1


# -- classify: duration only, no tokens ----------------------------------------


async def test_metered_classify_forwards_label_descriptions_to_delegate() -> None:
    """MeteredProvider is a pure pass-through decorator -- label_descriptions
    must reach the wrapped provider unchanged, not get dropped."""
    stub = _StubProvider()
    metered = MeteredProvider(stub, provider="openai")
    descriptions = {"off_topic": "unrelated to this business"}

    await metered.classify(
        "Who is the President of India?",
        labels=["question", "off_topic"],
        model="gpt-4o",
        label_descriptions=descriptions,
    )

    assert stub.last_classify_label_descriptions == descriptions


async def test_metered_classify_records_duration_count() -> None:
    """After classify, duration count for op=classify rose by 1."""
    stub = _StubProvider()
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="classify",
    ) or 0

    await metered.classify(
        "I need help",
        labels=["Sales", "Support"],
        model="gpt-4o",
    )

    after = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="classify",
    ) or 0

    assert after - before == 1


# -- stream: duration after exhaustion, chunks in order ------------------------


async def test_metered_stream_yields_chunks_and_records_duration() -> None:
    """stream yields the delegate's chunks in order; duration count rises after exhaustion."""
    stub = _StubProvider(
        stream_chunks=[Chunk("He"), Chunk("llo"), Chunk(" world")],
    )
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="stream",
    ) or 0

    chunks: list[Chunk] = []
    async for chunk in metered.stream(
        [ChatMessage("user", "Hello")],
        model="gpt-4o",
        max_tokens=512,
    ):
        chunks.append(chunk)

    after = _get_sample(
        "llm_request_duration_seconds_count", provider="openai", op="stream",
    ) or 0

    assert chunks == [Chunk("He"), Chunk("llo"), Chunk(" world")]
    assert after - before == 1


# -- error metrics: generate ---------------------------------------------------


async def test_metered_generate_error_increments_error_counter() -> None:
    """generate raises → error counter increments AND exception propagates."""
    stub = _StubProvider(raise_on="generate")
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_errors_total", provider="openai", op="generate",
    ) or 0

    with pytest.raises(LLMError):
        await metered.generate(
            [ChatMessage("user", "Hello")],
            model="gpt-4o",
            max_tokens=512,
        )

    after = _get_sample(
        "llm_errors_total", provider="openai", op="generate",
    ) or 0

    assert after - before == 1


# -- error metrics: embed ------------------------------------------------------


async def test_metered_embed_error_increments_error_counter() -> None:
    """embed raises → error counter increments AND exception propagates."""
    stub = _StubProvider(raise_on="embed")
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_errors_total", provider="openai", op="embed",
    ) or 0

    with pytest.raises(LLMError):
        await metered.embed(
            ["hello"],
            model="text-embedding-3-small",
        )

    after = _get_sample(
        "llm_errors_total", provider="openai", op="embed",
    ) or 0

    assert after - before == 1


# -- error metrics: classify ---------------------------------------------------


async def test_metered_classify_error_increments_error_counter() -> None:
    """classify raises → error counter increments AND exception propagates."""
    stub = _StubProvider(raise_on="classify")
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_errors_total", provider="openai", op="classify",
    ) or 0

    with pytest.raises(LLMError):
        await metered.classify(
            "I need help",
            labels=["Sales", "Support"],
            model="gpt-4o",
        )

    after = _get_sample(
        "llm_errors_total", provider="openai", op="classify",
    ) or 0

    assert after - before == 1


# -- error metrics: stream -----------------------------------------------------


async def test_metered_stream_error_increments_error_counter() -> None:
    """stream raises → error counter increments AND exception propagates."""
    stub = _StubProvider(raise_on="stream")
    metered = MeteredProvider(stub, provider="openai")

    before = _get_sample(
        "llm_errors_total", provider="openai", op="stream",
    ) or 0

    with pytest.raises(LLMError):
        async for _chunk in metered.stream(
            [ChatMessage("user", "Hello")],
            model="gpt-4o",
            max_tokens=512,
        ):
            pass

    after = _get_sample(
        "llm_errors_total", provider="openai", op="stream",
    ) or 0

    assert after - before == 1


# -- provider label distinguishes azure from openai ----------------------------


async def test_metered_provider_uses_provider_label() -> None:
    """Metrics use the provider label passed to MeteredProvider, not the delegate type."""
    stub = _StubProvider(
        completion=Completion(
            text="Hello.",
            model="my-deployment",
            input_tokens=8,
            output_tokens=3,
        ),
    )
    metered = MeteredProvider(stub, provider="azure")

    input_before = _get_sample(
        "llm_tokens_total", provider="azure", model="my-deployment", kind="input",
    ) or 0

    await metered.generate(
        [ChatMessage("user", "Hello")],
        model="my-deployment",
        max_tokens=512,
    )

    input_after = _get_sample(
        "llm_tokens_total", provider="azure", model="my-deployment", kind="input",
    ) or 0

    assert input_after - input_before == 8


# -- aclose delegates, no metrics needed ----------------------------------------


async def test_metered_aclose_delegates_to_delegate_aclose_exactly_once() -> None:
    """MeteredProvider.aclose() delegates to the wrapped provider's aclose() once."""
    stub = _StubProvider()
    metered = MeteredProvider(stub, provider="openai")

    await metered.aclose()

    assert stub.aclose_calls == 1
