"""Unit tests for the LLM provider factory.

Covers:
- openai config (with base_url) → MeteredProvider wrapping OpenAICompatibleProvider.
- anthropic config → MeteredProvider wrapping AnthropicProvider.
- azure config (with base_url + api_version) → MeteredProvider wrapping AzureOpenAIProvider.
- azure missing api_version → LLMError.
- azure missing base_url → LLMError.
- factory threads max_retries and timeout from settings into provider constructors.
- factory threads embedding_batch_size into OpenAICompatibleProvider only (S12.6).
- factory returns MeteredProvider for all valid configs.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from api.llm.anthropic_provider import AnthropicProvider
from api.llm.azure_provider import AzureOpenAIProvider
from api.llm.config_repository import LLMConfig
from api.llm.factory import provider_for
from api.llm.metered_provider import MeteredProvider
from api.llm.openai_provider import OpenAICompatibleProvider
from api.llm.provider import LLMError


def _mock_settings() -> object:
    """Return a mock settings object with default resilience values."""
    m: object = object()
    return m


def test_openai_config_yields_metered_openai_provider() -> None:
    """provider='openai' → MeteredProvider wrapping OpenAICompatibleProvider."""
    config = LLMConfig(
        provider="openai",
        model="gpt-4o",
        api_key="sk-test-key",
        base_url="https://opencode.ai/zen/v1",
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 2
        mock_settings.return_value.llm_timeout_seconds = 30.0
        provider = provider_for(config)
    assert isinstance(provider, MeteredProvider)
    assert isinstance(provider._delegate, OpenAICompatibleProvider)


def test_openai_config_without_base_url() -> None:
    """provider='openai' without base_url → MeteredProvider (None base_url)."""
    config = LLMConfig(
        provider="openai",
        model="gpt-4o",
        api_key="sk-test-key",
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 2
        mock_settings.return_value.llm_timeout_seconds = 30.0
        provider = provider_for(config)
    assert isinstance(provider, MeteredProvider)
    assert isinstance(provider._delegate, OpenAICompatibleProvider)


def test_anthropic_config_yields_metered_anthropic_provider() -> None:
    """provider='anthropic' → MeteredProvider wrapping AnthropicProvider."""
    config = LLMConfig(
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="sk-ant-test-key",
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 2
        mock_settings.return_value.llm_timeout_seconds = 30.0
        provider = provider_for(config)
    assert isinstance(provider, MeteredProvider)
    assert isinstance(provider._delegate, AnthropicProvider)


def test_azure_config_yields_metered_azure_provider() -> None:
    """provider='azure' → MeteredProvider wrapping AzureOpenAIProvider."""
    config = LLMConfig(
        provider="azure",
        model="my-deployment",
        api_key="sk-azure-key",
        base_url="https://my-resource.openai.azure.com",
        api_version="2024-02-01",
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 2
        mock_settings.return_value.llm_timeout_seconds = 30.0
        provider = provider_for(config)
    assert isinstance(provider, MeteredProvider)
    assert isinstance(provider._delegate, AzureOpenAIProvider)


def test_azure_missing_api_version_raises() -> None:
    """provider='azure' without api_version → LLMError."""
    config = LLMConfig(
        provider="azure",
        model="my-deployment",
        api_key="sk-azure-key",
        base_url="https://my-resource.openai.azure.com",
    )
    with pytest.raises(LLMError):
        provider_for(config)


def test_azure_missing_base_url_raises() -> None:
    """provider='azure' without base_url → LLMError."""
    config = LLMConfig(
        provider="azure",
        model="my-deployment",
        api_key="sk-azure-key",
        api_version="2024-02-01",
    )
    with pytest.raises(LLMError):
        provider_for(config)


def test_factory_returns_metered_provider() -> None:
    """Any valid config → MeteredProvider."""
    for provider_name, config in [
        ("openai", LLMConfig(provider="openai", model="gpt-4o", api_key="sk-key")),
        ("anthropic", LLMConfig(provider="anthropic", model="claude-opus-4-8", api_key="sk-key")),
        ("azure", LLMConfig(
            provider="azure", model="dep", api_key="sk-key",
            base_url="https://x.openai.azure.com", api_version="2024-02-01",
        )),
    ]:
        with patch("api.llm.factory.get_api_settings") as mock_settings:
            mock_settings.return_value.llm_max_retries = 2
            mock_settings.return_value.llm_timeout_seconds = 30.0
            provider = provider_for(config)
        assert isinstance(provider, MeteredProvider), f"Failed for {provider_name}"


def test_factory_threads_retries_and_timeout_to_openai() -> None:
    """OpenAICompatibleProvider receives max_retries and timeout from settings."""
    config = LLMConfig(
        provider="openai",
        model="gpt-4o",
        api_key="sk-test-key",
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 5
        mock_settings.return_value.llm_timeout_seconds = 45.0
        mock_settings.return_value.embedding_batch_size = 5
        with patch(
            "api.llm.factory.OpenAICompatibleProvider",
            autospec=True,
        ) as MockProvider:
            provider_for(config)
            MockProvider.assert_called_once_with(
                api_key="sk-test-key",
                base_url=None,
                max_retries=5,
                timeout=45.0,
                embedding_batch_size=5,
                embedding_base_url=None,
                embedding_api_key=None,
                embedding_dimensions=None,
            )


def test_factory_threads_embedding_api_key_and_dimensions_to_openai() -> None:
    """A tenant with a distinct embedding provider/key (SR-26, e.g. real
    OpenAI for embeddings while chat stays on Ollama) has BOTH passed
    through unchanged to OpenAICompatibleProvider."""
    chat_key_value = "ollama"
    chat_key = chat_key_value + "-key-unrelated"
    embedding_key = "sk-openai-embedding-secret-key"
    config = LLMConfig(
        provider="openai",
        model="gpt-oss:20b",
        api_key=chat_key,
        base_url="https://ollama.com/v1",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key=embedding_key,
        embedding_dimensions=768,
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 2
        mock_settings.return_value.llm_timeout_seconds = 30.0
        mock_settings.return_value.embedding_batch_size = 5
        with patch(
            "api.llm.factory.OpenAICompatibleProvider",
            autospec=True,
        ) as MockProvider:
            provider_for(config)
            MockProvider.assert_called_once_with(
                api_key=chat_key,
                base_url="https://ollama.com/v1",
                max_retries=2,
                timeout=30.0,
                embedding_batch_size=5,
                embedding_base_url="https://api.openai.com/v1",
                embedding_api_key=embedding_key,
                embedding_dimensions=768,
            )


def test_factory_threads_retries_and_timeout_to_anthropic() -> None:
    """AnthropicProvider receives max_retries and timeout from settings."""
    config = LLMConfig(
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="sk-ant-key",
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 3
        mock_settings.return_value.llm_timeout_seconds = 20.0
        with patch(
            "api.llm.factory.AnthropicProvider",
            autospec=True,
        ) as MockProvider:
            provider_for(config)
            MockProvider.assert_called_once_with(
                api_key="sk-ant-key",
                max_retries=3,
                timeout=20.0,
            )


def test_factory_threads_retries_and_timeout_to_azure() -> None:
    """AzureOpenAIProvider receives max_retries and timeout from settings."""
    config = LLMConfig(
        provider="azure",
        model="my-deployment",
        api_key="sk-azure-key",
        base_url="https://my-resource.openai.azure.com",
        api_version="2024-02-01",
    )
    with patch("api.llm.factory.get_api_settings") as mock_settings:
        mock_settings.return_value.llm_max_retries = 4
        mock_settings.return_value.llm_timeout_seconds = 50.0
        with patch(
            "api.llm.factory.AzureOpenAIProvider",
            autospec=True,
        ) as MockProvider:
            provider_for(config)
            MockProvider.assert_called_once_with(
                api_key="sk-azure-key",
                azure_endpoint="https://my-resource.openai.azure.com",
                api_version="2024-02-01",
                max_retries=4,
                timeout=50.0,
            )
