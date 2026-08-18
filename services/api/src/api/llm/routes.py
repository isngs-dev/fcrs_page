"""Debug LLM routes -- set tenant config and generate completions.

These are TEMPORARY debug endpoints (prefixed ``/debug/llm``) to prove the
provider boundary. Real admin-facing endpoints land in Phase 12.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from common.auth import AuthClaims, Role
from common.errors import ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.auth.dependencies import require_roles
from api.config import get_api_settings
from api.llm.config_repository import get_llm_config, upsert_llm_config
from api.llm.factory import provider_for
from api.llm.provider import ChatMessage

_log = get_logger(__name__)

router = APIRouter(prefix="/debug/llm", tags=["llm"])


class LLMConfigRequest(BaseModel):
    """Body for POST /debug/llm/config.

    For Ollama-backed OpenAI-compatible endpoints, use:
    {
        "provider": "openai",
        "model": "gpt-oss:20b",
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1"
    }
    Ollama does not require a real API key; the key is only sent in the HTTP
    header for compatibility with OpenAI-style clients and is stored encrypted
    in the tenant_llm_configs table.
    """

    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    api_version: str | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_dimensions: int | None = None


class GenerateRequest(BaseModel):
    """Body for POST /debug/llm/generate."""

    prompt: str


class EmbedRequest(BaseModel):
    """Body for POST /debug/llm/embed."""

    texts: list[str]
    model: str


class ClassifyRequest(BaseModel):
    """Body for POST /debug/llm/classify."""

    text: str
    labels: list[str]


class StreamRequest(BaseModel):
    """Body for POST /debug/llm/stream."""

    prompt: str


@router.post("/config")
async def set_llm_config(
    body: LLMConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, str | int | None]:
    """Set the calling tenant's LLM provider + model + API key.

    Returns provider + model only; every key (chat and embedding) is
    encrypted and never echoed.
    """
    await upsert_llm_config(
        request.app.state.db,
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
    _log.info(
        "LLM config updated",
        extra={
            "event": "llm_config_set",
            "provider": body.provider,
            "model": body.model,
        },
    )
    response: dict[str, str | int | None] = {
        "provider": body.provider,
        "model": body.model,
    }
    if body.embedding_model is not None:
        response["embedding_model"] = body.embedding_model
    if body.embedding_base_url is not None:
        response["embedding_base_url"] = body.embedding_base_url
    if body.embedding_dimensions is not None:
        response["embedding_dimensions"] = body.embedding_dimensions
    return response


@router.post("/generate")
async def generate(
    body: GenerateRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, object]:
    """Generate a completion using the tenant's configured LLM.

    Returns 422 ``LLM_NOT_CONFIGURED`` if the tenant has no config.
    Returns 502 ``LLM_ERROR`` if the provider call fails.
    """
    settings = get_api_settings()
    db = request.app.state.db

    config = await get_llm_config(db, claims)
    if config is None:
        raise ValidationError(
            "LLM is not configured for this tenant.",
            code="LLM_NOT_CONFIGURED",
        )

    provider = provider_for(config)
    try:
        completion = await provider.generate(
            [ChatMessage("user", body.prompt)],
            model=config.model,
            max_tokens=settings.llm_max_tokens,
        )
    finally:
        await provider.aclose()

    return {
        "text": completion.text,
        "model": completion.model,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
    }


@router.post("/embed")
async def embed(
    body: EmbedRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, object]:
    """Generate embeddings using the tenant's configured LLM.

    Returns 422 ``LLM_NOT_CONFIGURED`` if the tenant has no config.
    Returns 502 ``LLM_ERROR`` if the provider does not support embeddings
    (e.g. Anthropic) or the upstream call fails.
    """
    db = request.app.state.db

    config = await get_llm_config(db, claims)
    if config is None:
        raise ValidationError(
            "LLM is not configured for this tenant.",
            code="LLM_NOT_CONFIGURED",
        )

    provider = provider_for(config)
    try:
        vectors = await provider.embed(body.texts, model=body.model)
    finally:
        await provider.aclose()

    return {
        "model": body.model,
        "count": len(vectors),
        "dimension": len(vectors[0]) if vectors else 0,
    }


@router.post("/classify")
async def classify(
    body: ClassifyRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> dict[str, str]:
    """Classify text using the tenant's configured LLM.

    Returns 422 ``LLM_NOT_CONFIGURED`` if the tenant has no config.
    Returns 502 ``LLM_ERROR`` if the provider fails or the reply is not
    one of the provided labels.
    """
    db = request.app.state.db

    config = await get_llm_config(db, claims)
    if config is None:
        raise ValidationError(
            "LLM is not configured for this tenant.",
            code="LLM_NOT_CONFIGURED",
        )

    provider = provider_for(config)
    try:
        label = await provider.classify(body.text, body.labels, model=config.model)
    finally:
        await provider.aclose()

    return {"label": label, "model": config.model}


@router.post("/stream")
async def stream(
    body: StreamRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> StreamingResponse:
    """Stream a completion using the tenant's configured LLM.

    Returns 422 ``LLM_NOT_CONFIGURED`` if the tenant has no config.
    Returns 502 ``LLM_ERROR`` if the provider call fails.
    Response is plain concatenated text deltas (not SSE).
    """
    settings = get_api_settings()
    db = request.app.state.db

    config = await get_llm_config(db, claims)
    if config is None:
        raise ValidationError(
            "LLM is not configured for this tenant.",
            code="LLM_NOT_CONFIGURED",
        )

    provider = provider_for(config)
    stream_iter = provider.stream(
        [ChatMessage("user", body.prompt)],
        model=config.model,
        max_tokens=settings.llm_max_tokens,
    )

    async def _yield_text() -> AsyncIterator[str]:
        try:
            async for chunk in stream_iter:
                yield chunk.text
        finally:
            await provider.aclose()

    return StreamingResponse(_yield_text(), media_type="text/plain")
