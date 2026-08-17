"""OpenAI-compatible provider implementation.

Uses the async ``openai`` SDK. Does NOT send ``temperature`` or ``top_p``.
Supports any OpenAI-wire endpoint (OpenAI, OpenCode Zen, local Ollama) via
an optional ``base_url``.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from common.logging import get_logger
from openai import APIError

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


class OpenAICompatibleProvider:
    """OpenAI-wire backend for ``LLMProvider.generate``, ``embed``, ``classify``, and ``stream``."""

    # Class-level default so subclasses that override __init__ without calling
    # super().__init__() (e.g. AzureOpenAIProvider) still have this attribute
    # set -- see api/llm/azure_provider.py. Overridden per-instance below.
    _embedding_batch_size: int = 5

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float = 30.0,
        embedding_batch_size: int = 5,
        client: Any | None = None,
        embedding_base_url: str | None = None,
        embed_client: Any | None = None,
    ) -> None:
        self._embedding_batch_size = embedding_batch_size
        if client is not None:
            self._client = client
        else:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=max_retries,
                timeout=timeout,
            )

        # SR-24: embeddings can target a different OpenAI-wire endpoint than
        # chat/classify/stream (e.g. a self-hosted companion container for a
        # model with no hosted API). A separate AsyncOpenAI client is built
        # only when embedding_base_url is actually set -- every existing
        # tenant (embedding_base_url unset) keeps using the SAME client
        # object for embed as for everything else, unchanged.
        if embed_client is not None:
            self._embed_client = embed_client
        elif embedding_base_url is not None:
            from openai import AsyncOpenAI
            self._embed_client = AsyncOpenAI(
                api_key=api_key,
                base_url=embedding_base_url,
                max_retries=max_retries,
                timeout=timeout,
            )
        else:
            self._embed_client = self._client

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": m.role, "content": m.content} for m in messages],
            )
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=openai op=generate"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

        if not resp.choices:
            raise LLMError("LLM request failed.")

        choice = resp.choices[0]
        text = choice.message.content or ""
        return Completion(
            text=text,
            model=model,
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            stop_reason=choice.finish_reason,
        )

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
    ) -> list[Vector]:
        """Embed ``texts`` in fixed-size sub-batches, preserving order.

        Sending an entire document's chunk list as one unbatched
        ``embeddings.create()`` call was the root cause of ingestion timeouts
        on large documents (see
        dev_plan/HANDOFF_embedding_batch_timeout_fix.md). Each batch is
        capped at ``self._embedding_batch_size`` texts; batches are issued
        sequentially (not concurrently) to keep at most one upstream request
        in flight, matching prior behavior and avoiding rate-limit pressure.
        Any batch failure fails the whole call immediately (fail-fast, no
        partial/silently-incomplete vector list) -- see the handoff doc §6b
        for why the handbook's "retry chunk / mark failed / continue"
        fallback was explicitly rejected.
        """
        vectors: list[Vector] = []
        batch_size = self._embedding_batch_size
        total_batches = -(-len(texts) // batch_size) if batch_size > 0 else 0

        for batch_index, start in enumerate(range(0, len(texts), batch_size)):
            batch = texts[start : start + batch_size]
            batch_t0 = time.monotonic()
            try:
                resp = await self._embed_client.embeddings.create(
                    model=model,
                    input=batch,
                )
            except APIError as exc:
                _log.warning(
                    "LLM upstream call failed: provider=openai op=embed model=%s"
                    " status=%s detail=%s",
                    model,
                    getattr(exc, "status_code", None),
                    str(exc),
                )
                raise LLMError("LLM request failed.") from exc

            if not resp.data:
                raise LLMError("LLM request failed.")

            vectors.extend(d.embedding for d in resp.data)

            elapsed_ms = int((time.monotonic() - batch_t0) * 1000)
            _log.info(
                "LLM embed batch completed: provider=openai model=%s"
                " batch_index=%d batch_size=%d total_batches=%d elapsed_ms=%d",
                model,
                batch_index,
                len(batch),
                total_batches,
                elapsed_ms,
            )

        return vectors

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
        chat_messages = [
            ChatMessage("system", instruction),
            ChatMessage("user", text),
        ]

        try:
            resp = await self._client.chat.completions.create(
                model=model,
                max_tokens=_CLASSIFY_MAX_TOKENS,
                messages=[{"role": m.role, "content": m.content} for m in chat_messages],
            )
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=openai op=classify"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

        if not resp.choices:
            raise LLMError("LLM request failed.")

        reply = (resp.choices[0].message.content or "").strip()
        matched = match_label(reply, labels)
        if matched is not None:
            if matched.lower() != reply.lower():
                _log.info(
                    "LLM classify matched label via fallback tolerance:"
                    " provider=%s model=%s label=%s reply=%r",
                    "openai",
                    model,
                    matched,
                    reply[:_CLASSIFY_REPLY_LOG_LIMIT],
                )
            return matched
        _log.warning(
            "LLM classify produced no matching label: provider=%s model=%s labels=%s reply=%r",
            "openai",
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
            stream = await self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                stream=True,
            )
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=openai op=stream"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

        try:
            async for event in stream:
                if event.choices:
                    delta = event.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        yield Chunk(text=content)
        except APIError as exc:
            _log.warning(
                "LLM upstream call failed: provider=openai op=stream"
                " model=%s status=%s detail=%s",
                model,
                getattr(exc, "status_code", None),
                str(exc),
            )
            raise LLMError("LLM request failed.") from exc

    async def aclose(self) -> None:
        """Close the underlying ``AsyncOpenAI``/``AsyncAzureOpenAI`` client(s).

        Verified against the installed SDK: ``AsyncOpenAI`` (and its
        subclass ``AsyncAzureOpenAI``) expose ``async def close(self) ->
        None`` at ``openai/_base_client.py`` (``AsyncAPIClient.close``),
        which calls ``await self._client.aclose()`` on the wrapped
        ``httpx.AsyncClient``. When ``embedding_base_url`` was set,
        ``_embed_client`` is a DISTINCT client instance and must be closed
        too; when it wasn't, ``_embed_client is self._client`` (the common
        case for every existing tenant) so this closes it exactly once.
        """
        await self._client.close()
        if self._embed_client is not self._client:
            await self._embed_client.close()
