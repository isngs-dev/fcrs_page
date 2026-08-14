"""MeteredProvider -- decorator that records Prometheus metrics around an LLMProvider.

Implements ``LLMProvider`` structurally and delegates to a concrete provider.
Records:
- ``LLM_REQUEST_DURATION`` (Histogram, labels provider + op) for every call.
- ``LLM_ERRORS`` (Counter, labels provider + op) on exception, then re-raises.
- ``LLM_TOKENS`` (Counter, labels provider + model + kind) for ``generate`` only
  (input and output token counts from the ``Completion``).

``stream`` is a non-async method returning ``AsyncIterator[Chunk]`` to match the
Protocol. The inner async generator records duration after exhaustion and errors
mid-iteration.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

from api.llm.metrics import LLM_ERRORS, LLM_REQUEST_DURATION, LLM_TOKENS
from api.llm.provider import ChatMessage, Chunk, Completion, Label, LLMProvider, Vector


class MeteredProvider:
    """Decorator that records Prometheus metrics around any ``LLMProvider``."""

    def __init__(self, delegate: LLMProvider, *, provider: str) -> None:
        self._delegate = delegate
        self._provider = provider

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        try:
            with LLM_REQUEST_DURATION.labels(
                provider=self._provider, op="generate",
            ).time():
                completion = await self._delegate.generate(
                    messages, model=model, max_tokens=max_tokens,
                )
        except Exception:
            LLM_ERRORS.labels(provider=self._provider, op="generate").inc()
            raise

        LLM_TOKENS.labels(
            provider=self._provider, model=completion.model, kind="input",
        ).inc(completion.input_tokens)
        LLM_TOKENS.labels(
            provider=self._provider, model=completion.model, kind="output",
        ).inc(completion.output_tokens)
        return completion

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
    ) -> list[Vector]:
        try:
            with LLM_REQUEST_DURATION.labels(
                provider=self._provider, op="embed",
            ).time():
                return await self._delegate.embed(texts, model=model)
        except Exception:
            LLM_ERRORS.labels(provider=self._provider, op="embed").inc()
            raise

    async def classify(
        self,
        text: str,
        labels: list[str],
        *,
        model: str,
        label_descriptions: dict[str, str] | None = None,
    ) -> Label:
        try:
            with LLM_REQUEST_DURATION.labels(
                provider=self._provider, op="classify",
            ).time():
                return await self._delegate.classify(
                    text, labels, model=model, label_descriptions=label_descriptions,
                )
        except Exception:
            LLM_ERRORS.labels(provider=self._provider, op="classify").inc()
            raise

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> AsyncIterator[Chunk]:
        async def _gen() -> AsyncIterator[Chunk]:
            start = time.perf_counter()
            try:
                async for chunk in self._delegate.stream(
                    messages, model=model, max_tokens=max_tokens,
                ):
                    yield chunk
            except Exception:
                LLM_ERRORS.labels(
                    provider=self._provider, op="stream",
                ).inc()
                raise
            finally:
                elapsed = time.perf_counter() - start
                LLM_REQUEST_DURATION.labels(
                    provider=self._provider, op="stream",
                ).observe(elapsed)

        return _gen()

    async def aclose(self) -> None:
        """Delegate to the wrapped provider's ``aclose`` -- no metrics needed."""
        await self._delegate.aclose()
