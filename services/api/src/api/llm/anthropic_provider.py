"""Anthropic provider implementation.

Uses the async ``anthropic`` SDK. Does NOT send ``temperature``, ``top_p``,
``top_k``, or ``thinking``/``budget_tokens`` (they return 400 on
``claude-opus-4-8``).

Anthropic has no embeddings API -- ``embed`` raises an explicit ``LLMError``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from anthropic import APIError
from common.logging import get_logger

from api.llm.classify_matching import build_classify_instruction, match_label
from api.llm.provider import (
    ChatMessage,
    Chunk,
    Completion,
    Label,
    LLMError,
    Vector,
)

_log = get_logger(__name__)

_CLASSIFY_MAX_TOKENS = 256
_CLASSIFY_REPLY_LOG_LIMIT = 120


class AnthropicProvider:
    """Anthropic Claude backend for ``LLMProvider.generate``, ``classify``, and ``stream``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        max_retries: int = 2,
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(
                api_key=api_key,
                max_retries=max_retries,
                timeout=timeout,
            )

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": m.role, "content": m.content} for m in messages],
            )
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=anthropic op=generate"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

        text = "".join(b.text for b in resp.content if b.type == "text")
        return Completion(
            text=text,
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason,
        )

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
    ) -> list[Vector]:
        raise LLMError(
            "Embeddings are not supported by the Anthropic provider; "
            "configure an OpenAI-compatible embeddings endpoint.",
        )

    async def classify(
        self,
        text: str,
        labels: list[str],
        *,
        model: str,
        label_descriptions: dict[str, str] | None = None,
    ) -> Label:
        if not labels:
            raise LLMError("LLM request failed.")

        instruction = build_classify_instruction(labels, label_descriptions)

        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=_CLASSIFY_MAX_TOKENS,
                system=instruction,
                messages=[{"role": "user", "content": text}],
            )
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=anthropic op=classify"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

        reply = "".join(b.text for b in resp.content if b.type == "text").strip()
        matched = match_label(reply, labels)
        if matched is not None:
            if matched.lower() != reply.lower():
                _log.info(
                    "LLM classify matched label via fallback tolerance:"
                    " provider=%s model=%s label=%s reply=%r",
                    "anthropic",
                    model,
                    matched,
                    reply[:_CLASSIFY_REPLY_LOG_LIMIT],
                )
            return matched
        _log.warning(
            "LLM classify produced no matching label: provider=%s model=%s labels=%s reply=%r",
            "anthropic",
            model,
            labels,
            reply[:_CLASSIFY_REPLY_LOG_LIMIT],
        )
        raise LLMError("LLM request failed.")

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> AsyncIterator[Chunk]:
        try:
            stream = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                stream=True,
            )
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=anthropic op=stream"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

        try:
            async for event in stream:
                if (
                    getattr(event, "type", None) == "content_block_delta"
                    and getattr(event.delta, "type", None) == "text_delta"
                ):
                    yield Chunk(text=event.delta.text)
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=anthropic op=stream"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

    async def aclose(self) -> None:
        """Close the underlying ``AsyncAnthropic`` client.

        Verified against the installed SDK: ``AsyncAnthropic`` overrides
        ``async def close(self) -> None`` at ``anthropic/_client.py``
        (calling ``super().close()``, i.e. ``AsyncAPIClient.close`` in
        ``anthropic/_base_client.py``, which calls ``await
        self._client.aclose()`` on the wrapped ``httpx.AsyncClient``).
        """
        await self._client.close()
