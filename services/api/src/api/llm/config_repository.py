"""Per-tenant LLM config repository -- encrypted at rest.

``api_key_ciphertext`` is stored encrypted (AES-256-GCM via ``SecretBox``) and
decrypted only when building a provider.
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.crypto import SecretBox
from common.db import Database
from common.errors import ValidationError

from api.config import get_api_settings


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str  # DECRYPTED
    base_url: str | None = None
    api_version: str | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None


async def get_llm_config(db: Database, claims: AuthClaims) -> LLMConfig | None:
    """Fetch the tenant's LLM config, decrypting the API key.

    Raises ``ValidationError`` for global callers (PLATFORM_ADMIN) since LLM
    config is tenant-scoped.
    """
    if claims.tenant_id is None:
        raise ValidationError("LLM config is tenant-scoped.")

    row = await db.fetchrow(
        "SELECT provider, model, api_key_ciphertext, base_url, api_version, "
        "embedding_model, embedding_base_url "
        "FROM tenant_llm_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if row is None:
        return None

    box = SecretBox(get_api_settings().secret_encryption_key)
    api_key = box.decrypt_str(str(row["api_key_ciphertext"]))
    return LLMConfig(
        provider=str(row["provider"]),
        model=str(row["model"]),
        api_key=api_key,
        base_url=str(row["base_url"]) if row["base_url"] is not None else None,
        api_version=str(row["api_version"]) if row["api_version"] is not None else None,
        embedding_model=(
            str(row["embedding_model"]) if row.get("embedding_model") is not None else None
        ),
        embedding_base_url=(
            str(row["embedding_base_url"])
            if row.get("embedding_base_url") is not None
            else None
        ),
    )


async def upsert_llm_config(
    db: Database,
    claims: AuthClaims,
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None = None,
    api_version: str | None = None,
    embedding_model: str | None = None,
    embedding_base_url: str | None = None,
) -> None:
    """Insert or update the tenant's LLM config, encrypting the API key.

    Raises ``ValidationError`` for global callers.
    """
    if claims.tenant_id is None:
        raise ValidationError("LLM config is tenant-scoped.")

    box = SecretBox(get_api_settings().secret_encryption_key)
    ciphertext = box.encrypt(api_key)

    await db.execute(
        "INSERT INTO tenant_llm_configs "
        "(tenant_id, provider, model, api_key_ciphertext, base_url, api_version, "
        "embedding_model, embedding_base_url) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "provider = $2, model = $3, api_key_ciphertext = $4, "
        "base_url = $5, api_version = $6, embedding_model = $7, "
        "embedding_base_url = $8, updated_at = now()",
        claims.tenant_id,
        provider,
        model,
        ciphertext,
        base_url,
        api_version,
        embedding_model,
        embedding_base_url,
    )
