"""Azure OpenAI provider implementation.

Subclasses ``OpenAICompatibleProvider`` and overrides only ``__init__`` to
build an ``AsyncAzureOpenAI`` client. All operations (generate/embed/classify/
stream) are inherited unchanged — Azure is OpenAI-wire-compatible.

Note: ``model`` is the Azure **deployment name**, not a model identifier.
"""
from __future__ import annotations

from typing import Any

from api.llm.openai_provider import OpenAICompatibleProvider
from api.llm.provider import LLMError


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Azure OpenAI backend — inherits generate/embed/classify/stream from OpenAI."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        azure_endpoint: str | None = None,
        api_version: str | None = None,
        max_retries: int = 2,
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            from openai import AsyncAzureOpenAI
            if not azure_endpoint or not api_version:
                raise LLMError(
                    "Azure requires base_url (azure_endpoint) and api_version.",
                )
            self._client = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=azure_endpoint,
                api_version=api_version,
                max_retries=max_retries,
                timeout=timeout,
            )
        # SR-24's embedding_base_url override is openai-provider-only (the
        # factory never passes it here) -- Azure always embeds through the
        # same client it chats through, same as every OpenAICompatibleProvider
        # tenant that leaves embedding_base_url unset.
        self._embed_client = self._client
