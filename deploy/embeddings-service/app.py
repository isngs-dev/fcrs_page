"""SR-24 companion embedding container.

Self-hosted, OpenAI-wire-compatible ``POST /v1/embeddings`` backed by
``sentence-transformers/all-MiniLM-L6-v2`` (free, open-weights, 384-dim).
Exists because no hosted API serves this model -- it is deliberately NOT one
of the platform's first-class LLM providers (anthropic/openai/azure). The
API's ``OpenAICompatibleProvider`` talks to this service exactly as it would
talk to OpenAI itself, via a tenant's ``embedding_base_url`` override, so no
new provider class was needed on the API side.

Internal-network-only (no host port published in compose) -- same trust
model as this stack's postgres/redis/pgbouncer, so no auth is enforced here.
Do not expose this service's port publicly.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_model: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    global _model
    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer(_MODEL_NAME)
    yield


app = FastAPI(lifespan=lifespan)


class EmbeddingsRequest(BaseModel):
    input: str | list[str]
    model: str = _MODEL_NAME


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/embeddings")
async def create_embeddings(body: EmbeddingsRequest) -> dict[str, Any]:
    texts = [body.input] if isinstance(body.input, str) else body.input

    vectors = _model.encode(texts)

    total_tokens = sum(len(t.split()) for t in texts)

    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vec.tolist(), "index": i}
            for i, vec in enumerate(vectors)
        ],
        "model": body.model,
        "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    }
