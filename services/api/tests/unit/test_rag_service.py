"""Unit tests for api.rag.service.retrieve.

Covers:
- Success path with a stub provider (correct-dim vector) + stub search_chunks
  (3 matches): embeds the query once with model=config.embedding_model,
  returns chunks ordered, confidence == top score (clamped).
- No embedding_model configured -> RAG_EMBEDDING_NOT_CONFIGURED (422),
  provider_for/embed never called.
- embed raises LLMError -> propagates (not swallowed, no partial result).
- Empty result set -> confidence == 0.0, chunks == [], no exception.
- Negative top similarity -> confidence floored at 0.0.
- k clamping: k=0 -> 1; k=9999 -> rag_max_top_k (20).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.llm.provider import LLMError
from api.rag.repository import ChunkMatch, KeywordMatch

_TENANT_ID = "tenant-abc"

_TEST_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


def _reset_modules() -> None:
    # Clear settings caches so a patched os.environ is re-read at call time.
    # Do NOT delete api.rag / api.config from sys.modules -- that splits the
    # module graph (collection-time imports in the repository/routes test files
    # bind the OLD module, so patching the reimported module misses) and
    # poisons later tests. api.rag.* read settings via get_api_settings() at
    # CALL time, so a cache-clear alone is sufficient. (See the S4.4/S5.1
    # api.config fallout -- same trap, different package.)
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims(tenant_id: str | None = _TENANT_ID, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


class _StubConfig:
    def __init__(self, embedding_model: str | None = "nomic-embed-text") -> None:
        self.embedding_model = embedding_model
        self.provider = "openai"
        self.model = "gpt-4o"


class _StubProvider:
    def __init__(self, vector: list[float], *, error: Exception | None = None) -> None:
        self._vector = vector
        self._error = error
        self.embed_calls: list[tuple[list[str], str]] = []
        self.aclose_calls = 0

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        self.embed_calls.append((texts, model))
        if self._error is not None:
            raise self._error
        return [self._vector]

    async def aclose(self) -> None:
        self.aclose_calls += 1


async def test_retrieve_success_orders_chunks_and_confidence_is_top_score() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        settings = get_api_settings()
        dim = settings.embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        matches = [
            ChunkMatch(doc_id="d1", chunk_id="c1", content="a", score=0.9),
            ChunkMatch(doc_id="d1", chunk_id="c2", content="b", score=0.5),
            ChunkMatch(doc_id="d2", chunk_id="c3", content="c", score=0.2),
        ]
        provider = _StubProvider([0.1] * dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=AsyncMock(return_value=matches)),
        ):
            result = await retrieve(object(), _claims(), "what can it do?", k=5)

    assert result.chunks == matches
    assert result.confidence == pytest.approx(0.9)
    assert provider.embed_calls == [(["what can it do?"], "nomic-embed-text")]
    # Resource-leak fix: _embed_query must close the provider after use.
    assert provider.aclose_calls == 1


async def test_retrieve_no_embedding_model_raises_422_and_provider_not_called() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        from api.rag.service import retrieve  # noqa: PLC0415

        provider_for_mock = AsyncMock()

        with (
            patch(
                "api.rag.service.get_llm_config",
                new=AsyncMock(return_value=_StubConfig(embedding_model=None)),
            ),
            patch("api.rag.service.provider_for", new=provider_for_mock),
        ):
            with pytest.raises(ValidationError) as exc_info:
                await retrieve(object(), _claims(), "hello", k=5)

    assert exc_info.value.code == "RAG_EMBEDDING_NOT_CONFIGURED"
    provider_for_mock.assert_not_called()


async def test_retrieve_no_config_raises_422() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        from api.rag.service import retrieve  # noqa: PLC0415

        with patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=None)):
            with pytest.raises(ValidationError) as exc_info:
                await retrieve(object(), _claims(), "hello", k=5)

    assert exc_info.value.code == "RAG_EMBEDDING_NOT_CONFIGURED"


async def test_retrieve_embed_llm_error_propagates() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider([0.1], error=LLMError("upstream failed"))
        search_mock = AsyncMock()

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=search_mock),
        ):
            with pytest.raises(LLMError):
                await retrieve(object(), _claims(), "hello", k=5)

    search_mock.assert_not_awaited()
    # Resource-leak fix: the provider must be closed even though embed()
    # raised -- the try/finally must still run its finally on the error path.
    assert provider.aclose_calls == 1


async def test_retrieve_empty_result_set_confidence_zero_no_exception() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider([0.1] * dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=AsyncMock(return_value=[])),
        ):
            result = await retrieve(object(), _claims(), "hello", k=5)

    assert result.chunks == []
    assert result.confidence == 0.0


async def test_retrieve_negative_top_score_confidence_floored_at_zero() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        matches = [ChunkMatch(doc_id="d1", chunk_id="c1", content="a", score=-0.3)]
        provider = _StubProvider([0.1] * dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=AsyncMock(return_value=matches)),
        ):
            result = await retrieve(object(), _claims(), "hello", k=5)

    assert result.confidence == 0.0
    # The raw per-chunk score is surfaced as-is (not clamped) per decision 4.
    assert result.chunks[0].score == pytest.approx(-0.3)


async def test_retrieve_k_clamps_below_minimum_to_one() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider([0.1] * dim)
        search_mock = AsyncMock(return_value=[])

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=search_mock),
        ):
            await retrieve(object(), _claims(), "hello", k=0)

    assert search_mock.await_args.kwargs["top_k"] == 1


async def test_retrieve_k_clamps_above_maximum_to_rag_max_top_k() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        settings = get_api_settings()
        dim = settings.embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider([0.1] * dim)
        search_mock = AsyncMock(return_value=[])

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=search_mock),
        ):
            await retrieve(object(), _claims(), "hello", k=9999)

    assert search_mock.await_args.kwargs["top_k"] == settings.rag_max_top_k


async def test_retrieve_dimension_mismatch_raises_internal_error() -> None:
    """A wrong-dim query vector is a misconfiguration -- fail loud, never search."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import InternalServerError  # noqa: PLC0415

        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider([0.1, 0.2])  # wrong dimension
        search_mock = AsyncMock()

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=search_mock),
        ):
            with pytest.raises(InternalServerError) as exc_info:
                await retrieve(object(), _claims(), "hello", k=5)

    assert exc_info.value.code == "EMBEDDING_DIM_MISMATCH"
    search_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# S6.2 hybrid: _rrf_fuse
# ---------------------------------------------------------------------------


def _vec(chunk_id: str, score: float, doc_id: str = "d1", content: str = "c") -> ChunkMatch:
    return ChunkMatch(doc_id=doc_id, chunk_id=chunk_id, content=content, score=score)


def _kw(chunk_id: str, rank: float, doc_id: str = "d1", content: str = "c") -> KeywordMatch:
    return KeywordMatch(doc_id=doc_id, chunk_id=chunk_id, content=content, rank=rank)


def test_rrf_fuse_chunk_in_both_lists_outranks_single_leg_hits() -> None:
    from api.rag.service import _rrf_fuse  # noqa: PLC0415

    vector = [_vec("c1", 0.9), _vec("c2", 0.8)]
    keyword = [_kw("c2", 0.5), _kw("c3", 0.4)]

    fused = _rrf_fuse(vector, keyword, rrf_k=60, k=3)

    assert [m.chunk_id for m in fused] == ["c2", "c1", "c3"]
    by_id = {m.chunk_id: m for m in fused}
    assert by_id["c2"].matched_by == ["vector", "keyword"]
    assert by_id["c1"].matched_by == ["vector"]
    assert by_id["c3"].matched_by == ["keyword"]
    assert by_id["c2"].score == pytest.approx(0.8)
    assert by_id["c1"].score == pytest.approx(0.9)
    assert by_id["c3"].score is None


def test_rrf_fuse_exact_rank_math() -> None:
    from api.rag.service import _rrf_fuse  # noqa: PLC0415

    fused = _rrf_fuse([_vec("c1", 0.5)], [], rrf_k=60, k=5)

    assert fused[0].rrf_score == pytest.approx(1.0 / 61.0)


def test_rrf_fuse_tie_break_by_chunk_id() -> None:
    from api.rag.service import _rrf_fuse  # noqa: PLC0415

    # Both are rank-1 in their own (single) list -> identical rrf_score -> tie.
    vector = [_vec("z", 0.9)]
    keyword = [_kw("a", 0.9)]

    fused = _rrf_fuse(vector, keyword, rrf_k=60, k=5)

    assert fused[0].rrf_score == pytest.approx(fused[1].rrf_score)
    assert [m.chunk_id for m in fused] == ["a", "z"]


def test_rrf_fuse_returns_at_most_k() -> None:
    from api.rag.service import _rrf_fuse  # noqa: PLC0415

    vector = [_vec("c1", 0.9), _vec("c2", 0.8), _vec("c3", 0.7)]
    keyword = [_kw("c4", 0.6), _kw("c5", 0.5)]

    fused = _rrf_fuse(vector, keyword, rrf_k=60, k=2)

    # c1 (vector rank1) and c4 (keyword rank1) tie at 1/61 -> chunk_id tie-break
    # picks c1 first, c4 second (both beat the rank-2/rank-3 entries).
    assert len(fused) == 2
    assert [m.chunk_id for m in fused] == ["c1", "c4"]


def test_rrf_fuse_empty_lists_returns_empty() -> None:
    from api.rag.service import _rrf_fuse  # noqa: PLC0415

    assert _rrf_fuse([], [], rrf_k=60, k=5) == []


# ---------------------------------------------------------------------------
# S6.2 hybrid: _compute_confidence
# ---------------------------------------------------------------------------


def test_compute_confidence_empty_vector_hits_is_zero() -> None:
    from api.rag.service import _compute_confidence  # noqa: PLC0415

    confidence = _compute_confidence(
        [], bounded_k=5, floor=0.35, w_top=0.6, w_margin=0.25, w_coverage=0.15
    )

    assert confidence == 0.0


def test_compute_confidence_single_hit() -> None:
    from api.rag.service import _compute_confidence  # noqa: PLC0415

    confidence = _compute_confidence(
        [_vec("c1", 0.8)], bounded_k=5, floor=0.35, w_top=0.6, w_margin=0.25, w_coverage=0.15
    )

    # top=0.8, second=0.0, margin=0.8, coverage=1/5=0.2
    expected = 0.6 * 0.8 + 0.25 * 0.8 + 0.15 * 0.2
    assert confidence == pytest.approx(expected)


def test_compute_confidence_margin_well_separated_beats_tie() -> None:
    from api.rag.service import _compute_confidence  # noqa: PLC0415

    separated = _compute_confidence(
        [_vec("c1", 0.9), _vec("c2", 0.1)],
        bounded_k=5,
        floor=0.35,
        w_top=0.6,
        w_margin=0.25,
        w_coverage=0.15,
    )
    tied = _compute_confidence(
        [_vec("c1", 0.9), _vec("c2", 0.9)],
        bounded_k=5,
        floor=0.35,
        w_top=0.6,
        w_margin=0.25,
        w_coverage=0.15,
    )

    # separated: margin=0.8, coverage=1/5 (only top clears the floor) -> 0.77
    # tied: margin=0.0, coverage=2/5 (both clear the floor) -> 0.60
    # The margin weight (0.25) dominates the coverage weight (0.15) here.
    assert separated > tied


def test_compute_confidence_coverage_more_hits_above_floor_is_higher() -> None:
    """Holding top/second (and therefore margin) fixed, an extra hit above the
    floor raises coverage and therefore confidence."""
    from api.rag.service import _compute_confidence  # noqa: PLC0415

    few = _compute_confidence(
        [_vec("c1", 0.5), _vec("c2", 0.1)],
        bounded_k=5,
        floor=0.35,
        w_top=0.6,
        w_margin=0.25,
        w_coverage=0.15,
    )
    more = _compute_confidence(
        [_vec("c1", 0.5), _vec("c2", 0.1), _vec("c3", 0.4)],
        bounded_k=5,
        floor=0.35,
        w_top=0.6,
        w_margin=0.25,
        w_coverage=0.15,
    )

    assert more > few


def test_compute_confidence_clamped_to_0_1() -> None:
    from api.rag.service import _compute_confidence  # noqa: PLC0415

    # A single hit with a score >> 1.0 must clamp top/second to 1.0 before
    # combining -- confidence never exceeds 1.0 (top=1, margin=1, coverage=1
    # with the single-hit bounded_k=1 -> weights sum to exactly 1.0).
    confidence = _compute_confidence(
        [_vec("c1", 5.0)],
        bounded_k=1,
        floor=0.35,
        w_top=0.6,
        w_margin=0.25,
        w_coverage=0.15,
    )

    assert confidence == pytest.approx(1.0)
    assert 0.0 <= confidence <= 1.0


def test_compute_confidence_negative_scores_floored() -> None:
    from api.rag.service import _compute_confidence  # noqa: PLC0415

    confidence = _compute_confidence(
        [_vec("c1", -0.5)], bounded_k=5, floor=0.35, w_top=0.6, w_margin=0.25, w_coverage=0.15
    )

    assert confidence == 0.0


# ---------------------------------------------------------------------------
# S6.2 hybrid: retrieve_hybrid
# ---------------------------------------------------------------------------


async def test_retrieve_hybrid_embeds_once_calls_both_legs_with_candidate_k() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        settings = get_api_settings()
        dim = settings.embedding_dimension

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        vector_matches = [_vec("c1", 0.9), _vec("c2", 0.5)]
        keyword_matches = [_kw("c2", 0.7), _kw("c3", 0.3)]
        provider = _StubProvider([0.1] * dim)
        search_mock = AsyncMock(return_value=vector_matches)
        keyword_mock = AsyncMock(return_value=keyword_matches)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=search_mock),
            patch("api.rag.service.keyword_search", new=keyword_mock),
        ):
            result = await retrieve_hybrid(object(), _claims(), "mystery shopping", k=5)

    assert provider.embed_calls == [(["mystery shopping"], "nomic-embed-text")]
    assert search_mock.await_args.kwargs["top_k"] == settings.rag_hybrid_candidate_k
    assert keyword_mock.await_args.kwargs["top_k"] == settings.rag_hybrid_candidate_k
    # Confidence derives from the vector list only (top=0.9).
    assert result.confidence == pytest.approx(
        0.6 * 0.9 + 0.25 * (0.9 - 0.5) + 0.15 * (2 / settings.rag_hybrid_candidate_k)
    )
    chunk_ids = [c.chunk_id for c in result.chunks]
    assert "c2" in chunk_ids  # matched by both legs -- present


async def test_retrieve_hybrid_no_embedding_model_raises_422_neither_leg_called() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        search_mock = AsyncMock()
        keyword_mock = AsyncMock()
        provider_for_mock = AsyncMock()

        with (
            patch(
                "api.rag.service.get_llm_config",
                new=AsyncMock(return_value=_StubConfig(embedding_model=None)),
            ),
            patch("api.rag.service.provider_for", new=provider_for_mock),
            patch("api.rag.service.search_chunks", new=search_mock),
            patch("api.rag.service.keyword_search", new=keyword_mock),
        ):
            with pytest.raises(ValidationError) as exc_info:
                await retrieve_hybrid(object(), _claims(), "hello", k=5)

    assert exc_info.value.code == "RAG_EMBEDDING_NOT_CONFIGURED"
    provider_for_mock.assert_not_called()
    search_mock.assert_not_awaited()
    keyword_mock.assert_not_awaited()


async def test_retrieve_hybrid_embed_llm_error_propagates_neither_leg_called() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        provider = _StubProvider([0.1], error=LLMError("upstream failed"))
        search_mock = AsyncMock()
        keyword_mock = AsyncMock()

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=search_mock),
            patch("api.rag.service.keyword_search", new=keyword_mock),
        ):
            with pytest.raises(LLMError):
                await retrieve_hybrid(object(), _claims(), "hello", k=5)

    search_mock.assert_not_awaited()
    keyword_mock.assert_not_awaited()


async def test_retrieve_hybrid_empty_both_legs_confidence_zero_empty_chunks() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        provider = _StubProvider([0.1] * dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=AsyncMock(return_value=[])),
            patch("api.rag.service.keyword_search", new=AsyncMock(return_value=[])),
        ):
            result = await retrieve_hybrid(object(), _claims(), "hello", k=5)

    assert result.chunks == []
    assert result.confidence == 0.0


async def test_retrieve_hybrid_k_clamps_below_minimum_to_one() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        provider = _StubProvider([0.1] * dim)
        vector_matches = [_vec("c1", 0.9), _vec("c2", 0.5)]

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=AsyncMock(return_value=vector_matches)),
            patch("api.rag.service.keyword_search", new=AsyncMock(return_value=[])),
        ):
            result = await retrieve_hybrid(object(), _claims(), "hello", k=0)

    assert len(result.chunks) == 1


async def test_retrieve_hybrid_k_clamps_above_maximum() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        settings = get_api_settings()
        dim = settings.embedding_dimension

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        provider = _StubProvider([0.1] * dim)
        search_mock = AsyncMock(return_value=[])
        keyword_mock = AsyncMock(return_value=[])

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=search_mock),
            patch("api.rag.service.keyword_search", new=keyword_mock),
        ):
            await retrieve_hybrid(object(), _claims(), "hello", k=9999)

    # candidate_k is fixed by settings.rag_hybrid_candidate_k regardless of k;
    # the requested k only bounds how many fused results come back.
    assert search_mock.await_args.kwargs["top_k"] == settings.rag_hybrid_candidate_k
    assert keyword_mock.await_args.kwargs["top_k"] == settings.rag_hybrid_candidate_k


# -- A call question is embedded as soon as they seem done (2026-11-19) ---------------------


async def _ask_after_prefetch(prefetched: str, asked: str, *, prefetch_tenant: str = _TENANT_ID,
                              provider: object | None = None) -> _StubProvider:
    """prefetch_embedding(prefetched) for ``prefetch_tenant``, then retrieve_hybrid(asked)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension
        from api.rag.service import prefetch_embedding, retrieve_hybrid  # noqa: PLC0415

        stub = provider or _StubProvider([0.1] * dim)
        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=stub),
            patch("api.rag.service.search_chunks", new=AsyncMock(return_value=[])),
            patch("api.rag.service.keyword_search", new=AsyncMock(return_value=[])),
        ):
            prefetch_embedding(object(), _claims(prefetch_tenant), prefetched)  # type: ignore[arg-type]
            await asyncio.sleep(0.01)  # the early lookup runs while Flux makes sure
            await retrieve_hybrid(object(), _claims(), asked, k=5)  # type: ignore[arg-type]
    return stub  # type: ignore[return-value]


async def test_a_question_embedded_early_is_not_embedded_again() -> None:
    q = f"do you fix roofs {uuid4().hex}"
    provider = await _ask_after_prefetch(q, q)
    assert provider.embed_calls == [([q], "nomic-embed-text")]  # once, started early


async def test_a_different_question_is_embedded_on_its_own() -> None:
    early, asked = f"do you fix roofs {uuid4().hex}", f"do you fix gutters {uuid4().hex}"
    provider = await _ask_after_prefetch(early, asked)
    assert [texts for texts, _ in provider.embed_calls] == [[early], [asked]]


async def test_another_tenants_early_lookup_is_never_used() -> None:
    q = f"how much is a repair {uuid4().hex}"
    provider = await _ask_after_prefetch(q, q, prefetch_tenant="tenant-other")
    assert [texts for texts, _ in provider.embed_calls] == [[q], [q]]  # tenant A asks afresh


async def test_a_failed_early_lookup_is_simply_tried_again() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

    class _FailsOnce(_StubProvider):
        async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
            self.embed_calls.append((texts, model))
            if len(self.embed_calls) == 1:
                raise LLMError("upstream hiccup")
            return [self._vector]

    q = f"what areas do you serve {uuid4().hex}"
    provider = await _ask_after_prefetch(q, q, provider=_FailsOnce([0.1] * dim))
    assert [texts for texts, _ in provider.embed_calls] == [[q], [q]]


async def test_a_word_or_two_is_not_looked_up_early() -> None:
    provider = await _ask_after_prefetch("yes please", f"is it free {uuid4().hex}")
    assert len(provider.embed_calls) == 1  # only the real question
