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
    embedding_api_key: str | None = None  # DECRYPTED
    embedding_dimensions: int | None = None


@dataclass(frozen=True)
class LLMConfigSummary:
    """Display-safe view of a tenant's LLM config -- NEVER key material, just
    whether a key is set. Backs the self-service "AI provider" settings UI
    (`GET /admin/llm/config`)."""

    provider: str
    model: str
    has_api_key: bool
    base_url: str | None = None
    api_version: str | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    has_embedding_api_key: bool = False
    embedding_dimensions: int | None = None


async def get_llm_config(db: Database, claims: AuthClaims) -> LLMConfig | None:
    """Fetch the tenant's LLM config, decrypting the API key.

    Raises ``ValidationError`` for global callers (PLATFORM_ADMIN) since LLM
    config is tenant-scoped.
    """
    if claims.tenant_id is None:
        raise ValidationError("LLM config is tenant-scoped.")

    row = await db.fetchrow(
        "SELECT provider, model, api_key_ciphertext, base_url, api_version, "
        "embedding_model, embedding_base_url, embedding_api_key_ciphertext, "
        "embedding_dimensions "
        "FROM tenant_llm_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if row is None:
        return None

    box = SecretBox(get_api_settings().secret_encryption_key)
    api_key = box.decrypt_str(str(row["api_key_ciphertext"]))
    embedding_api_key_ciphertext = row.get("embedding_api_key_ciphertext")
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
        embedding_api_key=(
            box.decrypt_str(str(embedding_api_key_ciphertext))
            if embedding_api_key_ciphertext is not None
            else None
        ),
        embedding_dimensions=(
            int(row["embedding_dimensions"])
            if row.get("embedding_dimensions") is not None
            else None
        ),
    )


async def get_llm_config_summary(db: Database, claims: AuthClaims) -> LLMConfigSummary | None:
    """Display-safe read: provider/model/URLs plus whether each key is SET --
    never the ciphertext column, never a decrypt. Backs the settings UI's
    prefill + "Currently configured" / "Not set" indicators.

    Raises ``ValidationError`` for global callers (PLATFORM_ADMIN).
    """
    if claims.tenant_id is None:
        raise ValidationError("LLM config is tenant-scoped.")

    row = await db.fetchrow(
        "SELECT provider, model, base_url, api_version, embedding_model, "
        "embedding_base_url, embedding_dimensions, "
        "(api_key_ciphertext IS NOT NULL) AS has_api_key, "
        "(embedding_api_key_ciphertext IS NOT NULL) AS has_embedding_api_key "
        "FROM tenant_llm_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if row is None:
        return None

    return LLMConfigSummary(
        provider=str(row["provider"]),
        model=str(row["model"]),
        has_api_key=bool(row["has_api_key"]),
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
        has_embedding_api_key=bool(row.get("has_embedding_api_key") or False),
        embedding_dimensions=(
            int(row["embedding_dimensions"])
            if row.get("embedding_dimensions") is not None
            else None
        ),
    )


async def copy_most_recent_llm_config_to_tenant(
    db: Database, *, account_id: str, new_tenant_id: str
) -> bool:
    """Best-effort default: when a new chatbot is created, copy the account's
    most-recently-updated sibling chatbot's LLM config to it (provider,
    model, both URLs, embedding fields, AND the encrypted key ciphertexts --
    copied byte-for-byte, never decrypted/re-encrypted, since every tenant's
    key is encrypted with the same platform-wide ``secret_encryption_key``,
    not a per-tenant one).

    Pre-fills a sensible starting point (editable via `/admin/llm/config`
    immediately after) instead of leaving a brand-new chatbot with
    ``EMBEDDING_NOT_CONFIGURED`` until someone manually sets it up --
    accepted UX gap this closes, per the user's explicit request.

    Scoped to ``account_id`` via a join on ``tenants.client_account_id`` --
    can only ever copy from ANOTHER chatbot in the SAME account, never
    across accounts. Returns ``True`` if a config was found and copied,
    ``False`` if the account has no chatbot with a config yet (nothing to
    copy -- not an error).

    Not itself RBAC-checked (no ``AuthClaims`` param): this is an internal
    helper called only from ``create_tenant_for_own_account`` right after
    THAT function's own RBAC check and account resolution -- never exposed
    as a route.
    """
    row = await db.fetchrow(
        "SELECT c.provider, c.model, c.api_key_ciphertext, c.base_url, c.api_version, "
        "c.embedding_model, c.embedding_base_url, c.embedding_api_key_ciphertext, "
        "c.embedding_dimensions "
        "FROM tenant_llm_configs c "
        "JOIN tenants t ON t.id = c.tenant_id "
        "WHERE t.client_account_id = $1 "
        "ORDER BY c.updated_at DESC "
        "LIMIT 1",
        account_id,
    )
    if row is None:
        return False

    await db.execute(
        "INSERT INTO tenant_llm_configs "
        "(tenant_id, provider, model, api_key_ciphertext, base_url, api_version, "
        "embedding_model, embedding_base_url, embedding_api_key_ciphertext, "
        "embedding_dimensions) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
        "ON CONFLICT (tenant_id) DO NOTHING",
        new_tenant_id,
        row["provider"],
        row["model"],
        row["api_key_ciphertext"],
        row["base_url"],
        row["api_version"],
        row.get("embedding_model"),
        row.get("embedding_base_url"),
        row.get("embedding_api_key_ciphertext"),
        row.get("embedding_dimensions"),
    )
    return True


async def upsert_llm_config(
    db: Database,
    claims: AuthClaims,
    *,
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    api_version: str | None = None,
    embedding_model: str | None = None,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
    embedding_dimensions: int | None = None,
) -> None:
    """Insert or update the tenant's LLM config, encrypting the API key(s).

    ``api_key``/``embedding_api_key`` are OPTIONAL on an UPDATE: omitting one
    (``None``) preserves the currently-stored encrypted value (``COALESCE``
    in the ``ON CONFLICT`` clause) rather than forcing every edit to re-paste
    a live secret. On a tenant's FIRST-EVER config (no existing row to
    preserve from), ``api_key`` is still required -- raises
    ``ValidationError(code="API_KEY_REQUIRED")`` if omitted, since the
    column is ``NOT NULL`` and there is nothing to fall back to.

    ``/debug/llm/config``'s request model keeps ``api_key: str`` required, so
    that route's behavior is unchanged by this -- optionality only matters
    to callers (the real ``/admin/llm/config`` route) that pass ``None``.

    Raises ``ValidationError`` for global callers.
    """
    if claims.tenant_id is None:
        raise ValidationError("LLM config is tenant-scoped.")

    if api_key is None:
        existing = await db.fetchrow(
            "SELECT tenant_id FROM tenant_llm_configs WHERE tenant_id = $1",
            claims.tenant_id,
        )
        if existing is None:
            raise ValidationError(
                "An API key is required the first time you configure a provider.",
                code="API_KEY_REQUIRED",
            )

    box = SecretBox(get_api_settings().secret_encryption_key)
    ciphertext = box.encrypt(api_key) if api_key is not None else None
    embedding_ciphertext = box.encrypt(embedding_api_key) if embedding_api_key is not None else None

    await db.execute(
        "INSERT INTO tenant_llm_configs "
        "(tenant_id, provider, model, api_key_ciphertext, base_url, api_version, "
        "embedding_model, embedding_base_url, embedding_api_key_ciphertext, "
        "embedding_dimensions) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "provider = $2, model = $3, "
        "api_key_ciphertext = COALESCE($4, tenant_llm_configs.api_key_ciphertext), "
        "base_url = $5, api_version = $6, embedding_model = $7, "
        "embedding_base_url = $8, "
        "embedding_api_key_ciphertext = "
        "COALESCE($9, tenant_llm_configs.embedding_api_key_ciphertext), "
        "embedding_dimensions = $10, updated_at = now()",
        claims.tenant_id,
        provider,
        model,
        ciphertext,
        base_url,
        api_version,
        embedding_model,
        embedding_base_url,
        embedding_ciphertext,
        embedding_dimensions,
    )
