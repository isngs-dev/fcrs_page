"""RAG service -- embed the query, search the tenant's chunks, score confidence.

``retrieve`` is the read-side counterpart of ingestion (S5.3): embed an
incoming query via the tenant's own provider + embedding model, run a
tenant-scoped pgvector cosine search, and return the top-k chunks plus a
single ``confidence`` signal. No silent fallback: a misconfigured or failing
embedding step fails loud, and an empty knowledge base returns a low-confidence
empty result rather than an error or a fabricated chunk.
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.db import Database
from common.errors import InternalServerError, ValidationError

from api.config import get_api_settings
from api.llm.config_repository import get_llm_config
from api.llm.factory import provider_for
from api.rag.repository import ChunkMatch, KeywordMatch, keyword_search, search_chunks


@dataclass(frozen=True)
class RetrievalResult:
    """The result of a single retrieval call."""

    chunks: list[ChunkMatch]
    confidence: float


@dataclass(frozen=True)
class HybridMatch:
    """A single fused (RRF) result surfacing its provenance.

    ``score`` is the cosine similarity when the chunk was in the vector list
    (``None`` for a keyword-only hit -- honest, never back-filled with a fake
    distance). ``rrf_score`` is the fusion value (debugging/ordering
    transparency, not independently meaningful). ``matched_by`` is
    ``["vector"]``, ``["keyword"]``, or ``["vector", "keyword"]``.
    """

    doc_id: str
    chunk_id: str
    content: str
    score: float | None
    rrf_score: float
    matched_by: list[str]
    category: str = "general"  # plan 2.6: the document's category (pricing rule)


@dataclass(frozen=True)
class HybridResult:
    """The result of a single hybrid (vector + keyword) retrieval call."""

    chunks: list[HybridMatch]
    confidence: float


def _clamp_k(k: int, *, max_k: int) -> int:
    """Clamp ``k`` to ``[1, max_k]`` -- an unbounded/huge k must not run."""
    if k < 1:
        return 1
    if k > max_k:
        return max_k
    return k


async def retrieve(
    db: Database,
    claims: AuthClaims,
    query: str,
    *,
    k: int,
) -> RetrievalResult:
    """Embed ``query`` with the tenant's provider and search their chunks.

    Raises
    ------
    ValidationError (``RAG_EMBEDDING_NOT_CONFIGURED``)
        If the tenant has no LLM config or no ``embedding_model`` set. Never
        embed with a guessed model, never fabricate a zero vector.
    InternalServerError (``EMBEDDING_DIM_MISMATCH``)
        If the embedded query vector's dimension does not match
        ``settings.embedding_dimension`` -- a mismatched query model is a
        misconfiguration, fail loud rather than bind a malformed vector.
    LLMError
        Propagates untouched from ``provider.embed`` (transient upstream
        failure) -- never swallowed, never a partial result.

    An empty result set (no chunks for the tenant, or empty KB) returns
    ``confidence=0.0`` and ``chunks=[]`` -- never raises.
    """
    settings = get_api_settings()
    bounded_k = _clamp_k(k, max_k=settings.rag_max_top_k)

    qvec = await _embed_query(db, claims, query, embedding_dimension=settings.embedding_dimension)

    chunks = await search_chunks(db, claims, qvec, top_k=bounded_k)

    # confidence = top-1 score clamped to [0.0, 1.0]; empty result -> 0.0.
    confidence = max(0.0, min(1.0, chunks[0].score)) if chunks else 0.0

    return RetrievalResult(chunks=chunks, confidence=confidence)


async def _embed_query(
    db: Database,
    claims: AuthClaims,
    query: str,
    *,
    embedding_dimension: int,
) -> list[float]:
    """Embed ``query`` with the tenant's own provider + embedding model.

    Shared by ``retrieve`` (S6.1) and ``retrieve_hybrid`` (S6.2) -- both need
    exactly the same fail-loud embedding step before any search runs.

    Raises
    ------
    ValidationError (``RAG_EMBEDDING_NOT_CONFIGURED``)
        No LLM config or no ``embedding_model`` set for this tenant.
    InternalServerError (``EMBEDDING_DIM_MISMATCH``)
        The embedded query vector's dimension does not match
        ``embedding_dimension``.
    LLMError
        Propagates untouched from ``provider.embed``.
    """
    config = await get_llm_config(db, claims)
    if config is None or not config.embedding_model:
        raise ValidationError(
            "No embedding model is configured for this tenant.",
            code="RAG_EMBEDDING_NOT_CONFIGURED",
        )

    provider = provider_for(config)
    try:
        # LLMError from embed propagates untouched -- transient, never swallowed.
        vectors = await provider.embed([query], model=config.embedding_model)
    finally:
        await provider.aclose()
    qvec = vectors[0]

    if len(qvec) != embedding_dimension:
        raise InternalServerError(
            f"Query embedding has dimension {len(qvec)}, "
            f"expected {embedding_dimension}.",
            code="EMBEDDING_DIM_MISMATCH",
        )
    return qvec


def _rrf_fuse(
    vector_matches: list[ChunkMatch],
    keyword_matches: list[KeywordMatch],
    *,
    rrf_k: int,
    k: int,
) -> list[HybridMatch]:
    """Fuse the vector + keyword ranked lists via Reciprocal Rank Fusion.

    ``rrf_score(chunk) = sum over lists containing it of 1 / (rrf_k + rank)``
    where ``rank`` is the chunk's 1-based position within that list (both
    input lists are assumed already best-first). A chunk present in both
    lists accumulates both terms. Returns the top ``k`` by ``rrf_score`` desc,
    tie-broken by ``chunk_id`` ascending for determinism.

    Cosine similarity and ``ts_rank`` are not on a comparable scale, so
    fusion is by rank position, never by raw score (decision 2, S6.2).
    """
    rrf_scores: dict[str, float] = {}
    doc_ids: dict[str, str] = {}
    contents: dict[str, str] = {}
    categories: dict[str, str] = {}
    scores: dict[str, float | None] = {}
    matched_by: dict[str, list[str]] = {}

    for rank, vm in enumerate(vector_matches, start=1):
        rrf_scores[vm.chunk_id] = rrf_scores.get(vm.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
        doc_ids.setdefault(vm.chunk_id, vm.doc_id)
        contents.setdefault(vm.chunk_id, vm.content)
        categories.setdefault(vm.chunk_id, vm.category)
        scores[vm.chunk_id] = vm.score
        matched_by.setdefault(vm.chunk_id, []).append("vector")

    for rank, km in enumerate(keyword_matches, start=1):
        rrf_scores[km.chunk_id] = rrf_scores.get(km.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
        doc_ids.setdefault(km.chunk_id, km.doc_id)
        contents.setdefault(km.chunk_id, km.content)
        categories.setdefault(km.chunk_id, km.category)
        scores.setdefault(km.chunk_id, None)
        matched_by.setdefault(km.chunk_id, []).append("keyword")

    ordered = sorted(rrf_scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        HybridMatch(
            doc_id=doc_ids[chunk_id],
            chunk_id=chunk_id,
            content=contents[chunk_id],
            score=scores[chunk_id],
            rrf_score=rrf_score,
            matched_by=matched_by[chunk_id],
            category=categories[chunk_id],
        )
        for chunk_id, rrf_score in ordered[:k]
    ]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _compute_confidence(
    vector_matches: list[ChunkMatch],
    *,
    bounded_k: int,
    floor: float,
    w_top: float,
    w_margin: float,
    w_coverage: float,
) -> float:
    """The richer hybrid confidence signal -- semantic strength only.

    Derived from the VECTOR list only (fusion changes ordering/recall, not
    confidence; decision 4, S6.2):

    - ``top``      = clamp01(vector_matches[0].score) if any hits else 0.0
    - ``second``   = clamp01(vector_matches[1].score) if >=2 hits else 0.0
    - ``margin``   = max(0.0, top - second)
    - ``coverage`` = (count of vector hits with score >= floor) / bounded_k
    - ``confidence`` = clamp01(w_top*top + w_margin*margin + w_coverage*coverage)

    Empty vector hits -> confidence 0.0. Pure function; all inputs are
    config-driven (tunable per deployment).
    """
    if not vector_matches:
        return 0.0

    top = _clamp01(vector_matches[0].score)
    second = _clamp01(vector_matches[1].score) if len(vector_matches) >= 2 else 0.0
    margin = max(0.0, top - second)
    covered = sum(1 for m in vector_matches if m.score >= floor)
    coverage = (covered / bounded_k) if bounded_k > 0 else 0.0

    confidence = w_top * top + w_margin * margin + w_coverage * coverage
    return _clamp01(confidence)


async def retrieve_hybrid(
    db: Database,
    claims: AuthClaims,
    query: str,
    *,
    k: int,
) -> HybridResult:
    """Embed ``query`` once, search both legs, fuse (RRF), score confidence.

    Both legs (vector via ``search_chunks``, keyword via ``keyword_search``)
    are searched at ``settings.rag_hybrid_candidate_k`` depth -- a candidate
    pool wider than the final ``k`` -- then fused and truncated to the
    (clamped) requested ``k``. Confidence is computed from the vector leg
    alone (see ``_compute_confidence``), using the candidate-pool size as
    its coverage denominator (the depth actually searched).

    Same failure taxonomy as ``retrieve`` (decision 5, S6.2): missing
    ``embedding_model`` -> ``RAG_EMBEDDING_NOT_CONFIGURED`` (422) before
    either leg runs; ``embed`` ``LLMError`` propagates untouched; empty
    results on both legs -> ``confidence=0.0``, ``chunks=[]``, never raises.
    """
    settings = get_api_settings()
    bounded_k = _clamp_k(k, max_k=settings.rag_max_top_k)
    candidate_k = settings.rag_hybrid_candidate_k

    qvec = await _embed_query(db, claims, query, embedding_dimension=settings.embedding_dimension)

    vector_matches = await search_chunks(db, claims, qvec, top_k=candidate_k)
    keyword_matches = await keyword_search(db, claims, query, top_k=candidate_k)

    fused = _rrf_fuse(vector_matches, keyword_matches, rrf_k=settings.rag_rrf_k, k=bounded_k)
    confidence = _compute_confidence(
        vector_matches,
        bounded_k=candidate_k,
        floor=settings.rag_confidence_floor,
        w_top=settings.rag_conf_w_top,
        w_margin=settings.rag_conf_w_margin,
        w_coverage=settings.rag_conf_w_coverage,
    )

    return HybridResult(chunks=fused, confidence=confidence)
