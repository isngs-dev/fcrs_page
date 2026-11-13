"""Anthropic provider implementation.

Uses the async ``anthropic`` SDK. Does NOT send ``temperature``, ``top_p``,
``top_k``, or ``thinking``/``budget_tokens`` (they return 400 on
``claude-opus-4-8``).

Anthropic has no embeddings API -- ``embed`` uses the tenant's OpenAI-compatible
embeddings endpoint when one is configured (the knowledge base was embedded there), else
raises an explicit ``LLMError``. Leading ``system`` messages go to the ``system`` field;
``reasoning_effort`` maps to each model's own thinking/effort controls (2026-11-13).
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

# Models that take ``output_config.effort`` (Claude 4.6 and later).
_EFFORT_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable",
                  "claude-mythos", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                  "claude-sonnet-4-6")
# Thinking can't be turned off on these -- only lowered with effort.
_ALWAYS_THINKING = ("claude-opus-5-5", "claude-fable", "claude-mythos")
# Server-side refusal fallback (Anthropic's guidance: on by default for these).
_FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _speed(model: str, effort: str | None) -> dict[str, Any]:
    """The caller's speed hint as this model's own controls. ``"none"`` (a live call) = the
    fastest first word the model allows: thinking off where it can be (Sonnet 5.5 spells it
    ``between_tools``), always at low effort. No hint, or an older model: nothing sent."""
    if not effort or not model.startswith(_EFFORT_MODELS):
        return {}
    if effort != "none":
        return {"output_config": {"effort": effort}}
    out: dict[str, Any] = {"output_config": {"effort": "low"}}
    if model.startswith("claude-sonnet-5-5"):
        out["thinking"] = {"type": "between_tools"}
    elif not model.startswith(_ALWAYS_THINKING):
        out["thinking"] = {"type": "disabled"}
    return out


class AnthropicProvider:
    """Anthropic Claude backend for ``LLMProvider.generate``, ``classify``, and ``stream``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        max_retries: int = 2,
        timeout: float = 30.0,
        client: Any | None = None,
        embedder: Any | None = None,
    ) -> None:
        self._embedder = embedder  # the tenant's OpenAI-compatible embeddings provider
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(
                api_key=api_key,
                max_retries=max_retries,
                timeout=timeout,
            )

    def _request(self, messages: list[ChatMessage], *, model: str, max_tokens: int,
                 reasoning_effort: str | None, stream: bool = False) -> tuple[Any, dict[str, Any]]:
        """(the create call, its arguments): system messages in ``system``, the speed hint
        as the model's controls, and the server-side refusal fallback where advised."""
        kwargs: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content}
                         for m in messages if m.role != "system"],
            **_speed(model, reasoning_effort),
        }
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        if system:
            kwargs["system"] = system
        if stream:
            kwargs["stream"] = True
        if model.startswith(_FALLBACK_MODELS):
            return self._client.beta.messages.create, {
                **kwargs, "betas": [_FALLBACK_BETA], "fallbacks": "default"}
        return self._client.messages.create, kwargs

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> Completion:
        create, kwargs = self._request(messages, model=model, max_tokens=max_tokens,
                                       reasoning_effort=reasoning_effort)
        try:
            resp = await create(**kwargs)
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=anthropic op=generate"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

        # A declined answer is never passed on as text: callers treat it as a miss.
        text = ("" if resp.stop_reason == "refusal"
                else "".join(b.text for b in resp.content if b.type == "text"))
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
        if self._embedder is not None:
            vectors: list[Vector] = await self._embedder.embed(texts, model=model)
            return vectors
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
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[Chunk]:
        create, kwargs = self._request(messages, model=model, max_tokens=max_tokens,
                                       reasoning_effort=reasoning_effort, stream=True)
        try:
            stream = await create(**kwargs)
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
        if self._embedder is not None:
            await self._embedder.aclose()
