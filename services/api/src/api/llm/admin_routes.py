"""Real admin-facing LLM provider config -- GET/POST /admin/llm/config.

CLIENT_ADMIN-only, own tenant, no PLATFORM_ADMIN tenant-scoped mirror
(matches the precedent already set for knowledge upload, chatbot deletion,
and add-a-website this cycle: platform admins don't get new mutation
capability into a client's tenant). Mirrors `api_keys_routes.py`'s
paired-router + shared-`_impl` structure.

This is a genuinely NEW route, not a promotion of `/debug/llm/config`
(`llm/routes.py`) -- that route is left exactly as-is; its Pydantic model
still requires `api_key`, so its behavior is unaffected by
`upsert_llm_config` gaining optional-key support here.

Credential hygiene: the raw/decrypted key is NEVER read back by this file --
`get_llm_config_summary` (the only read this router calls) structurally
cannot return key material, only `has_api_key`/`has_embedding_api_key`
booleans.
"""
from __future__ import annotations

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles
from api.llm.config_repository import (
    LLMConfigSummary,
    get_llm_config_summary,
    upsert_llm_config,
)

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/llm", tags=["llm"])

_ALLOWED_PROVIDERS = ("anthropic", "openai", "azure")


class LLMConfigSummaryResponse(BaseModel):
    """Response for GET and POST /admin/llm/config. NEVER carries key material."""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_version: str | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_dimensions: int | None = None
    has_api_key: bool = False
    has_embedding_api_key: bool = False


class SetLLMConfigRequest(BaseModel):
    """Body for POST /admin/llm/config.

    ``api_key``/``embedding_api_key`` are optional: omitted/blank preserves
    the currently-stored key (see ``upsert_llm_config``'s docstring) --
    required only the first time a tenant configures a provider (422
    ``API_KEY_REQUIRED`` otherwise).
    """

    provider: str = Field(pattern="^(" + "|".join(_ALLOWED_PROVIDERS) + ")$")
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4000)
    base_url: str | None = Field(default=None, max_length=500)
    api_version: str | None = Field(default=None, max_length=50)
    embedding_model: str | None = Field(default=None, max_length=200)
    embedding_base_url: str | None = Field(default=None, max_length=500)
    embedding_api_key: str | None = Field(default=None, max_length=4000)
    embedding_dimensions: int | None = Field(default=None, gt=0, le=8192)


def _to_response(summary: LLMConfigSummary | None) -> LLMConfigSummaryResponse:
    if summary is None:
        return LLMConfigSummaryResponse()
    return LLMConfigSummaryResponse(
        provider=summary.provider,
        model=summary.model,
        base_url=summary.base_url,
        api_version=summary.api_version,
        embedding_model=summary.embedding_model,
        embedding_base_url=summary.embedding_base_url,
        embedding_dimensions=summary.embedding_dimensions,
        has_api_key=summary.has_api_key,
        has_embedding_api_key=summary.has_embedding_api_key,
    )


@router.get("/config")
async def get_llm_config_route(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> LLMConfigSummaryResponse:
    """No provider configured yet -> an all-null/false response, never a 404
    (matches the rest of this app's "honest empty state" convention)."""
    db = request.app.state.db
    summary = await get_llm_config_summary(db, claims)
    return _to_response(summary)


@router.post("/config")
async def set_llm_config_route(
    body: SetLLMConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> LLMConfigSummaryResponse:
    """422 ``API_KEY_REQUIRED`` if this is the tenant's first-ever config and
    ``api_key`` was left blank."""
    db = request.app.state.db

    await upsert_llm_config(
        db,
        claims,
        provider=body.provider,
        model=body.model,
        api_key=body.api_key,
        base_url=body.base_url,
        api_version=body.api_version,
        embedding_model=body.embedding_model,
        embedding_base_url=body.embedding_base_url,
        embedding_api_key=body.embedding_api_key,
        embedding_dimensions=body.embedding_dimensions,
    )

    await record_audit(
        db,
        claims,
        action="llm_config_updated",
        target_type="tenant",
        target_id=claims.tenant_id,
        metadata={"provider": body.provider, "model": body.model},
        actor_context=get_platform_admin_actor(request),
    )

    # Secret-minimal log line -- NEVER key material (mirrors api_keys_routes.py).
    _log.info(
        "LLM config updated via admin route",
        extra={"event": "llm_config_updated", "provider": body.provider},
    )

    summary = await get_llm_config_summary(db, claims)
    return _to_response(summary)
