"""LLM provider boundary -- provider-agnostic Protocol + domain types.

No default provider is configured. A tenant with no ``tenant_llm_configs`` row
gets an explicit error, never a fabricated answer.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from common.errors import AppException

Label = str
Vector = list[float]


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class Chunk:
    text: str


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None


class LLMProvider(Protocol):
    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion: ...

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
    ) -> list[Vector]: ...

    async def classify(
        self,
        text: str,
        labels: list[str],
        *,
        model: str,
        label_descriptions: dict[str, str] | None = None,
    ) -> Label: ...

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> AsyncIterator[Chunk]: ...

    async def aclose(self) -> None:
        """Release the underlying SDK client's connection pool.

        Every call site that obtains a provider via ``provider_for`` owns its
        lifetime and MUST call this exactly once when done -- providers wrap
        an ``httpx.AsyncClient`` (or equivalent) that is never closed
        implicitly. Idempotent implementations are encouraged but not
        required by this Protocol; callers close at most once per instance.
        """
        ...


class LLMError(AppException):
    """Provider-level failure (network, upstream error, unsupported provider)."""

    code = "LLM_ERROR"
    http_status = 502
    default_message = "LLM request failed."
