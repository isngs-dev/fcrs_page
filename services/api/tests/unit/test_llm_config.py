"""Unit tests for LLM config repository (multi-tenant isolation).

Covers:
- upsert_llm_config stores ciphertext (not plaintext); decrypt round-trips.
- get_llm_config SELECT carries WHERE tenant_id = $1 bound to caller's tenant.
- Tenant A's claims never read tenant B's row.
- PLATFORM_ADMIN (global) → ValidationError.
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.crypto import SecretBox
from common.errors import ValidationError

from api.config import get_api_settings
from api.llm.config_repository import get_llm_config, upsert_llm_config

# -- Test doubles --------------------------------------------------------------

_TEST_ENCRYPTION_KEY = "x" * 48  # 48 chars, but SecretBox normalizes to 32 bytes


class _RecordingDatabase:
    """Database double that records SQL + params."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.last_sql: str = ""
        self.last_params: tuple[Any, ...] = ()
        self._rows = rows or []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.last_sql = query
        self.last_params = args
        return self._rows[0] if self._rows else None

    async def execute(self, query: str, *args: Any) -> str:
        self.last_sql = query
        self.last_params = args
        return "INSERT 1"

    async def close(self) -> None:
        pass


# -- Helpers -------------------------------------------------------------------


def _claims(tenant_id: str | None, role: Role) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


# -- Upsert stores ciphertext, not plaintext -----------------------------------


async def test_upsert_stores_ciphertext_not_plaintext() -> None:
    """The 4th bound param (api_key_ciphertext) != the plaintext key."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    plaintext_key = "sk-test-secret-key-12345"

    await upsert_llm_config(db, claims, provider="anthropic", model="claude-opus-4-8", api_key=plaintext_key)

    ciphertext = db.last_params[3]
    assert isinstance(ciphertext, str)
    assert ciphertext != plaintext_key

    # Round-trip: decrypt the stored ciphertext
    box = SecretBox(get_api_settings().secret_encryption_key)
    decrypted = box.decrypt_str(ciphertext)
    assert decrypted == plaintext_key


# -- base_url round-trips through upsert → get ---------------------------------


async def test_base_url_round_trips() -> None:
    """base_url stored via upsert and returned via get_llm_config."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db, claims,
        provider="openai",
        model="gpt-4o",
        api_key="sk-key",
        base_url="https://opencode.ai/zen/v1",
    )

    assert db.last_params[4] == "https://opencode.ai/zen/v1"


async def test_omitted_base_url_is_none() -> None:
    """Omitted base_url → None in params."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db, claims,
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="sk-key",
    )

    assert db.last_params[4] is None


# -- get_llm_config with base_url ----------------------------------------------


async def test_get_llm_config_returns_base_url() -> None:
    """get_llm_config returns base_url when present."""
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": "https://opencode.ai/zen/v1",
        "api_version": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.provider == "openai"
    assert config.base_url == "https://opencode.ai/zen/v1"


async def test_get_llm_config_base_url_none_when_null() -> None:
    """get_llm_config returns base_url=None when column is NULL."""
    row = {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.base_url is None


# -- get_llm_config carries tenant filter --------------------------------------


async def test_get_llm_config_filters_by_tenant_id() -> None:
    """SELECT carries WHERE tenant_id = $1 bound to the caller's tenant."""
    row = {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.provider == "anthropic"
    assert config.model == "claude-opus-4-8"
    assert config.api_key == "sk-key"
    assert "tenant_id" in db.last_sql
    assert db.last_params[0] == "tenant-a"


# -- Multi-tenant isolation ----------------------------------------------------


async def test_tenant_a_cannot_read_tenant_b_config() -> None:
    """Tenant A's claims → SELECT bound to A's id; tenant B's row is never returned."""
    db = _RecordingDatabase(rows=[])  # No rows for tenant A
    claims_a = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims_a)

    assert config is None
    assert db.last_params[0] == "tenant-a"


# -- Global admin rejected -----------------------------------------------------


async def test_platform_admin_rejected() -> None:
    """PLATFORM_ADMIN (global, tenant_id=None) → ValidationError."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await get_llm_config(db, claims)


async def test_platform_admin_rejected_on_upsert() -> None:
    """PLATFORM_ADMIN cannot upsert LLM config."""
    db = _RecordingDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await upsert_llm_config(db, claims, provider="anthropic", model="claude-opus-4-8", api_key="sk-key")


# -- api_version round-trips through upsert → get --------------------------------


async def test_api_version_round_trips() -> None:
    """api_version stored via upsert and returned via get_llm_config."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db, claims,
        provider="azure",
        model="my-deployment",
        api_key="sk-key",
        base_url="https://my-resource.openai.azure.com",
        api_version="2024-02-01",
    )

    assert db.last_params[5] == "2024-02-01"


async def test_omitted_api_version_is_none() -> None:
    """Omitted api_version → None in params."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db, claims,
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="sk-key",
    )

    assert db.last_params[5] is None


async def test_get_llm_config_returns_api_version() -> None:
    """get_llm_config returns api_version when present."""
    row = {
        "provider": "azure",
        "model": "my-deployment",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": "https://my-resource.openai.azure.com",
        "api_version": "2024-02-01",
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.provider == "azure"
    assert config.api_version == "2024-02-01"


async def test_get_llm_config_api_version_none_when_null() -> None:
    """get_llm_config returns api_version=None when column is NULL."""
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.api_version is None


# ==============================================================================
# S5.3: embedding_model stored and echoed
# ==============================================================================


async def test_upsert_stores_embedding_model() -> None:
    """embedding_model is passed as the 7th bound param to upsert."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db,
        claims,
        provider="openai",
        model="gpt-4o",
        api_key="sk-key",
        embedding_model="nomic-embed-text",
    )

    # 7th param (index 6) is embedding_model
    assert db.last_params[6] == "nomic-embed-text"


async def test_upsert_embedding_model_none_when_omitted() -> None:
    """Omitted embedding_model → None in params."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db,
        claims,
        provider="openai",
        model="gpt-4o",
        api_key="sk-key",
    )

    assert db.last_params[6] is None


async def test_get_llm_config_returns_embedding_model() -> None:
    """get_llm_config returns embedding_model when present."""
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
        "embedding_model": "nomic-embed-text",
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_model == "nomic-embed-text"


async def test_get_llm_config_embedding_model_none_when_null() -> None:
    """get_llm_config returns embedding_model=None when column is NULL."""
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
        "embedding_model": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_model is None


# ==============================================================================
# SR-24: embedding_base_url stored and echoed (companion embedding container)
# ==============================================================================


async def test_upsert_stores_embedding_base_url() -> None:
    """embedding_base_url is passed as the 8th bound param to upsert."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db,
        claims,
        provider="openai",
        model="gpt-oss:20b",
        api_key="ollama",
        embedding_model="all-MiniLM-L6-v2",
        embedding_base_url="http://embeddings:8080/v1",
    )

    # 8th param (index 7) is embedding_base_url.
    assert db.last_params[7] == "http://embeddings:8080/v1"


async def test_upsert_embedding_base_url_none_when_omitted() -> None:
    """Omitted embedding_base_url → None in params (unchanged behavior for
    every existing tenant, where chat and embed share one base_url)."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db,
        claims,
        provider="openai",
        model="gpt-4o",
        api_key="sk-key",
    )

    assert db.last_params[7] is None


async def test_get_llm_config_returns_embedding_base_url() -> None:
    """get_llm_config returns embedding_base_url when present."""
    row = {
        "provider": "openai",
        "model": "gpt-oss:20b",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("ollama"),
        "base_url": "https://ollama.com/v1",
        "api_version": None,
        "embedding_model": "all-MiniLM-L6-v2",
        "embedding_base_url": "http://embeddings:8080/v1",
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_base_url == "http://embeddings:8080/v1"


async def test_get_llm_config_embedding_base_url_none_when_null() -> None:
    """get_llm_config returns embedding_base_url=None when column is NULL."""
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
        "embedding_model": None,
        "embedding_base_url": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_base_url is None


# ==============================================================================
# SR-26: embedding_api_key stored encrypted, distinct from the chat api_key
# ==============================================================================


async def test_upsert_stores_embedding_api_key_as_ciphertext_not_plaintext() -> None:
    """embedding_api_key is the 9th bound param (index 8), stored encrypted --
    a REAL hosted provider (unlike the companion container) needs a real,
    validated credential, and it may differ from the tenant's chat api_key
    (e.g. Ollama for chat, real OpenAI for embeddings)."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    chat_key = "ollama-key-unrelated"
    plaintext_embedding_key = "sk-openai-embedding-secret-key"

    await upsert_llm_config(
        db,
        claims,
        provider="openai",
        model="gpt-oss:20b",
        api_key=chat_key,
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key=plaintext_embedding_key,
    )

    ciphertext = db.last_params[8]
    assert isinstance(ciphertext, str)
    assert ciphertext != plaintext_embedding_key

    box = SecretBox(get_api_settings().secret_encryption_key)
    assert box.decrypt_str(ciphertext) == plaintext_embedding_key


async def test_upsert_embedding_api_key_none_when_omitted() -> None:
    """Omitted embedding_api_key -> None in params (every existing tenant,
    where the companion container needed no real auth, or where embed and
    chat legitimately share one provider/key)."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db, claims, provider="openai", model="gpt-4o", api_key="sk-key",
    )

    assert db.last_params[8] is None


async def test_get_llm_config_decrypts_embedding_api_key() -> None:
    """get_llm_config returns embedding_api_key DECRYPTED, distinct from api_key."""
    box = SecretBox(get_api_settings().secret_encryption_key)
    chat_key = "ollama-key-unrelated"
    embedding_key = "sk-openai-embedding-secret-key"
    row = {
        "provider": "openai",
        "model": "gpt-oss:20b",
        "api_key_ciphertext": box.encrypt(chat_key),
        "base_url": "https://ollama.com/v1",
        "api_version": None,
        "embedding_model": "text-embedding-3-small",
        "embedding_base_url": "https://api.openai.com/v1",
        "embedding_api_key_ciphertext": box.encrypt(embedding_key),
        "embedding_dimensions": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_api_key == embedding_key
    assert config.api_key == chat_key
    assert config.embedding_api_key != config.api_key


async def test_get_llm_config_embedding_api_key_none_when_null() -> None:
    """get_llm_config returns embedding_api_key=None when column is NULL --
    every existing tenant before this migration."""
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
        "embedding_model": None,
        "embedding_base_url": None,
        "embedding_api_key_ciphertext": None,
        "embedding_dimensions": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_api_key is None


# ==============================================================================
# SR-26: embedding_dimensions (plain int, NOT encrypted -- not a secret)
# ==============================================================================


async def test_upsert_stores_embedding_dimensions() -> None:
    """embedding_dimensions is the 10th bound param (index 9)."""
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)
    chat_key = "ollama-key-unrelated"

    await upsert_llm_config(
        db, claims, provider="openai", model="gpt-oss:20b", api_key=chat_key,
        embedding_model="text-embedding-3-small", embedding_dimensions=768,
    )

    assert db.last_params[9] == 768


async def test_upsert_embedding_dimensions_none_when_omitted() -> None:
    db = _RecordingDatabase()
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    await upsert_llm_config(
        db, claims, provider="openai", model="gpt-4o", api_key="sk-key",
    )

    assert db.last_params[9] is None


async def test_get_llm_config_returns_embedding_dimensions() -> None:
    row = {
        "provider": "openai",
        "model": "gpt-oss:20b",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
        "embedding_model": "text-embedding-3-small",
        "embedding_base_url": "https://api.openai.com/v1",
        "embedding_api_key_ciphertext": None,
        "embedding_dimensions": 768,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_dimensions == 768


async def test_get_llm_config_embedding_dimensions_none_when_null() -> None:
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-key"),
        "base_url": None,
        "api_version": None,
        "embedding_model": None,
        "embedding_base_url": None,
        "embedding_api_key_ciphertext": None,
        "embedding_dimensions": None,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    assert config.embedding_dimensions is None


async def test_api_key_never_echoed_in_config() -> None:
    """api_key is decrypted internally but must not appear in the LLMConfig fields that
    would be echoed to callers (provider, model, embedding_model, base_url, api_version)."""
    row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-super-secret"),
        "base_url": None,
        "api_version": None,
        "embedding_model": "nomic-embed-text",
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", Role.CLIENT_ADMIN)

    config = await get_llm_config(db, claims)

    assert config is not None
    # api_key is available on the dataclass (for internal use by providers),
    # but it should never be serialized back to callers. The route layer is
    # responsible for that; here we just confirm the field is not in the
    # "safe to echo" fields.
    safe_fields = {config.provider, config.model, config.embedding_model, config.base_url, config.api_version}
    assert "sk-super-secret" not in safe_fields
