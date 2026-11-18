"""RAG repository -- tenant-scoped pgvector similarity search over knowledge_chunks.

This is a thin wrapper around ``common.pgvector.similarity_search``. Tenant
isolation is already enforced inside ``similarity_search`` (it applies
``tenant_filter(claims, ...)``); this module adds no un-filtered query. The
only responsibility here is:

- ``_reject_global(claims)`` so a PLATFORM_ADMIN (global, ``tenant_id=None``)
  caller is rejected -- retrieval is always tenant-scoped.
- Mapping each returned ``Record`` (which carries a ``distance`` column, cosine
  distance in ``[0, 2]``) into a typed ``ChunkMatch`` with
  ``score = 1.0 - distance``.

Data model (migration 0011): ``knowledge_chunks(tenant_id PK, doc_id, chunk_id
PK, content, embedding vector(768), metadata jsonb, created_at)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError
from common.pgvector import similarity_search
from common.tenancy import tenant_filter

from api.config import get_api_settings


@dataclass(frozen=True)
class ChunkMatch:
    """A single retrieved chunk with its cosine-similarity score.

    ``score`` is the raw cosine similarity (``1 - distance``, range
    ``[-1, 1]``) -- NOT clamped here. The caller (``api.rag.service``) derives
    the single ``confidence`` signal from it; the per-chunk score is surfaced
    as-is so downstream consumers (e.g. the orchestrator) can see the raw
    value.
    """

    doc_id: str
    chunk_id: str
    content: str
    score: float
    category: str = "general"  # plan 2.6: the document's category


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    RAG retrieval is always tenant-scoped; a global caller has no tenant_id
    and therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "RAG retrieval is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def search_chunks(
    db: Database,
    claims: AuthClaims,
    embedding: list[float],
    *,
    top_k: int,
) -> list[ChunkMatch]:
    """Return the ``top_k`` knowledge_chunks most similar to ``embedding``.

    Tenant-filtered inside ``similarity_search``; results are ordered by
    ascending distance (= descending score), matching the DB's ``ORDER BY``.
    """
    _reject_global(claims)

    # Plan 2.6: only approved documents are ever retrieved (migration 0081's view).
    rows = await similarity_search(
        db,
        "approved_knowledge_chunks",
        claims,
        embedding,
        top_k=top_k,
        select="doc_id, chunk_id, content, category",
    )
    return [
        ChunkMatch(
            doc_id=str(row["doc_id"]),
            chunk_id=str(row["chunk_id"]),
            content=str(row["content"]),
            score=1.0 - float(row["distance"]),
            category=str(row["category"]),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class KeywordMatch:
    """A single keyword (Postgres FTS) match with its ``ts_rank`` score.

    ``rank`` is the raw ``ts_rank`` value -- not on a comparable scale to
    ``ChunkMatch.score`` (cosine similarity). Fusion (``api.rag.service``)
    combines the two lists by rank position (RRF), never by raw score.
    """

    doc_id: str
    chunk_id: str
    content: str
    rank: float
    category: str = "general"  # plan 2.6: the document's category


async def keyword_search(
    db: Database,
    claims: AuthClaims,
    query: str,
    *,
    top_k: int,
) -> list[KeywordMatch]:
    """Return the ``top_k`` knowledge_chunks best matching ``query`` via Postgres FTS.

    The FTS regconfig (``settings.rag_fts_language``), the query text, and the
    ``LIMIT`` are ALL bound (positional) parameters -- never string-interpolated
    into the SQL text. The regconfig is bound via ``$1::regconfig``. Tenant
    filtering reuses ``common.tenancy.tenant_filter`` (the same helper
    ``common.pgvector.similarity_search`` uses), so a dropped filter here would
    be caught by the same isolation-test discipline as the vector leg.
    """
    _reject_global(claims)

    settings = get_api_settings()
    params: list[Any] = [settings.rag_fts_language, query]
    frag, fparams = tenant_filter(claims, next_param=len(params) + 1)
    params += fparams
    params.append(top_k)
    limit_idx = len(params)
    sql = (
        "SELECT doc_id, chunk_id, content, category, "  # noqa: S608
        "ts_rank(to_tsvector($1::regconfig, content), plainto_tsquery($1::regconfig, $2)) AS rank "
        "FROM approved_knowledge_chunks "
        "WHERE to_tsvector($1::regconfig, content) @@ plainto_tsquery($1::regconfig, $2) "
        f"{frag} "
        f"ORDER BY rank DESC LIMIT ${limit_idx}"
    )
    rows = await db.fetch(sql, *params)
    return [
        KeywordMatch(
            doc_id=str(row["doc_id"]),
            chunk_id=str(row["chunk_id"]),
            content=str(row["content"]),
            rank=float(row["rank"]),
            category=str(row["category"]),
        )
        for row in rows
    ]


async def resolve_chunks(
    db: Database, claims: AuthClaims, chunk_ids: list[str],
) -> dict[str, str]:
    """Resolve chunk_ids to live content, tenant-scoped (PK = (tenant_id, chunk_id)).

    Used by the SR-2 grounding spot-check endpoint to resolve a bot message's
    historical, stored ``sources`` chunk_ids back to the real
    ``knowledge_chunks.content`` so a reviewer can judge groundedness for
    themselves. Returns a map ONLY for chunk_ids that resolve within the
    caller's tenant; an unresolved id (deleted/re-ingested chunk, or a
    cross-tenant id) is simply absent from the map -- the caller marks it
    content=null/resolved=false (no silent fallback, no placeholder text).

    The tenant filter is applied INSIDE this query (via
    ``common.tenancy.tenant_filter``, the same helper ``keyword_search``
    uses) -- not a post-fetch check -- so a chunk_id recorded in tenant A's
    historical sources that happens to equal a real tenant B chunk id cannot
    resolve to B's text: B's row simply fails the ``tenant_id = $N``
    predicate and is absent from the result set. This is the load-bearing
    cross-tenant-resolution guarantee for this sprint (SR-2 decision 4).

    Rejects global (PLATFORM_ADMIN) callers -- retrieval is tenant-scoped.
    Empty ``chunk_ids`` short-circuits to ``{}`` with no query issued.
    """
    _reject_global(claims)
    if not chunk_ids:
        return {}

    params: list[Any] = [chunk_ids]  # $1 (bound array, never interpolated)
    frag, fparams = tenant_filter(claims, next_param=len(params) + 1)
    params += fparams
    sql = (
        "SELECT chunk_id, content FROM knowledge_chunks "  # noqa: S608
        "WHERE chunk_id = ANY($1::text[]) "
        f"{frag}"
    )
    rows = await db.fetch(sql, *params)
    return {str(r["chunk_id"]): str(r["content"]) for r in rows}
