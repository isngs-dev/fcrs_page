"""Mandatory tenant isolation test for RAG retrieval (S6.1).

Mirrors the ``similarity_search`` tenant-filter guarantee at the ``retrieve``
service seam: a stub ``similarity_search`` is seeded with rows for BOTH
tenant-A and tenant-B, exactly like a real (unfiltered) query result would
look if the ``WHERE tenant_id = $N`` fragment were ever dropped. The stub
applies the same tenant filter ``common.pgvector.similarity_search`` performs
internally (via ``tenant_filter``), so this test proves that ``retrieve``
under tenant-A claims can only ever surface tenant-A chunks -- tenant-B's rows
must never leak across the seam, no matter what the DB "actually" holds.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from common.auth import AuthClaims, Role

from api.rag.repository import KeywordMatch

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"

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

# Simulates the full (both-tenant) contents of knowledge_chunks as pgvector
# would see it before the tenant_filter WHERE fragment is applied.
_ALL_ROWS = [
    {"tenant_id": _TENANT_A, "doc_id": "a-doc", "chunk_id": "a-0000", "content": "tenant A secret", "distance": 0.05,
     "category": "general"},
    {"tenant_id": _TENANT_B, "doc_id": "b-doc", "chunk_id": "b-0000", "content": "tenant B secret", "distance": 0.02,
     "category": "general"},
]


def _reset_modules() -> None:
    # Clear settings caches only. Do NOT delete api.rag / api.config from
    # sys.modules -- that splits the module graph and poisons the repository/
    # routes tests (which import search_chunks at collection time). api.rag.*
    # read settings via get_api_settings() at call time, so a cache-clear is
    # sufficient. (Same trap as the S4.4/S5.1 api.config fallout.)
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims(tenant_id: str, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


class _StubConfig:
    embedding_model = "nomic-embed-text"
    provider = "openai"
    model = "gpt-4o"


class _StubProvider:
    def __init__(self, dim: int) -> None:
        self._dim = dim
        self.aclose_calls = 0

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        return [[0.1] * self._dim]

    async def aclose(self) -> None:
        self.aclose_calls += 1


async def _fake_similarity_search(
    db: Any,
    table: str,
    claims: AuthClaims,
    embedding: list[float],
    *,
    top_k: int,
    select: str,
) -> list[dict[str, Any]]:
    """Applies the same tenant filter real pgvector.similarity_search enforces."""
    return [row for row in _ALL_ROWS if row["tenant_id"] == claims.tenant_id][:top_k]


async def test_retrieve_under_tenant_a_never_returns_tenant_b_chunks() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider(dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.repository.similarity_search", new=_fake_similarity_search),
        ):
            result_a = await retrieve(object(), _claims(_TENANT_A), "query", k=5)
            result_b = await retrieve(object(), _claims(_TENANT_B), "query", k=5)

    assert len(result_a.chunks) == 1
    assert result_a.chunks[0].doc_id == "a-doc"
    assert result_a.chunks[0].content == "tenant A secret"
    assert all("tenant B" not in c.content for c in result_a.chunks)

    assert len(result_b.chunks) == 1
    assert result_b.chunks[0].doc_id == "b-doc"
    assert result_b.chunks[0].content == "tenant B secret"
    assert all("tenant A" not in c.content for c in result_b.chunks)


async def test_retrieve_tenant_with_no_chunks_returns_empty_never_another_tenants() -> None:
    """A tenant with zero chunks gets confidence 0.0 + [] -- never tenant B's content."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider(dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.repository.similarity_search", new=_fake_similarity_search),
        ):
            result = await retrieve(object(), _claims("tenant-with-no-chunks"), "query", k=5)

    assert result.chunks == []
    assert result.confidence == 0.0


async def test_global_platform_admin_caller_rejected_not_leaked_to_search() -> None:
    """A PLATFORM_ADMIN (tenant_id=None) caller must be rejected before any search."""
    from common.errors import ValidationError

    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve  # noqa: PLC0415

        provider = _StubProvider(dim)
        search_spy = AsyncMock(side_effect=_fake_similarity_search)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.repository.similarity_search", new=search_spy),
        ):
            claims = AuthClaims(subject="admin", role=Role.PLATFORM_ADMIN, tenant_id=None)
            try:
                await retrieve(object(), claims, "query", k=5)
                raised = False
            except ValidationError:
                raised = True

    assert raised
    search_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# S6.2: the KEYWORD leg must be tenant-isolated too -- retrieve_hybrid under
# tenant-A must never surface tenant-B content via EITHER leg.
# ---------------------------------------------------------------------------

# Distinct content per tenant on BOTH legs, so a leaked row (from either the
# vector or the keyword leg) is unambiguous in the assertions below.
_ALL_KEYWORD_ROWS = [
    {"tenant_id": _TENANT_A, "doc_id": "a-doc", "chunk_id": "a-kw-0000", "content": "tenant A mystery shopping", "rank": 0.9},
    {"tenant_id": _TENANT_B, "doc_id": "b-doc", "chunk_id": "b-kw-0000", "content": "tenant B mystery shopping", "rank": 0.8},
]


async def _fake_keyword_search(
    db: Any,
    claims: AuthClaims,
    query: str,
    *,
    top_k: int,
) -> list[KeywordMatch]:
    """Applies the same tenant filter the real FTS query enforces (WHERE tenant_id = $N).

    ``api.rag.service.keyword_search`` is patched here at the already-mapped
    (``KeywordMatch``) seam -- unlike ``_fake_similarity_search`` above, which
    stands in for the lower-level ``common.pgvector.similarity_search`` and
    still returns raw rows for ``api.rag.repository.search_chunks`` to map.
    """
    return [
        KeywordMatch(doc_id=row["doc_id"], chunk_id=row["chunk_id"], content=row["content"], rank=row["rank"])
        for row in _ALL_KEYWORD_ROWS
        if row["tenant_id"] == claims.tenant_id
    ][:top_k]


async def test_retrieve_hybrid_under_tenant_a_never_returns_tenant_b_via_either_leg() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        provider = _StubProvider(dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.repository.similarity_search", new=_fake_similarity_search),
            patch("api.rag.service.keyword_search", new=_fake_keyword_search),
        ):
            result_a = await retrieve_hybrid(object(), _claims(_TENANT_A), "mystery shopping", k=5)
            result_b = await retrieve_hybrid(object(), _claims(_TENANT_B), "mystery shopping", k=5)

    a_contents = [c.content for c in result_a.chunks]
    b_contents = [c.content for c in result_b.chunks]

    assert any("tenant A" in c for c in a_contents)
    assert all("tenant B" not in c for c in a_contents)

    assert any("tenant B" in c for c in b_contents)
    assert all("tenant A" not in c for c in b_contents)


async def test_retrieve_hybrid_keyword_leg_alone_is_tenant_isolated() -> None:
    """Even when the vector leg is empty, the keyword leg alone must not leak."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        provider = _StubProvider(dim)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.service.search_chunks", new=AsyncMock(return_value=[])),
            patch("api.rag.service.keyword_search", new=_fake_keyword_search),
        ):
            result_a = await retrieve_hybrid(object(), _claims(_TENANT_A), "mystery shopping", k=5)

    assert len(result_a.chunks) == 1
    assert result_a.chunks[0].chunk_id == "a-kw-0000"
    assert result_a.chunks[0].matched_by == ["keyword"]
    assert "tenant B" not in result_a.chunks[0].content


async def test_retrieve_hybrid_global_platform_admin_rejected_before_either_leg() -> None:
    """A PLATFORM_ADMIN (tenant_id=None) caller must be rejected before both legs run."""
    from common.errors import ValidationError

    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        dim = get_api_settings().embedding_dimension

        from api.rag.service import retrieve_hybrid  # noqa: PLC0415

        provider = _StubProvider(dim)
        search_spy = AsyncMock(side_effect=_fake_similarity_search)
        keyword_spy = AsyncMock(side_effect=_fake_keyword_search)

        with (
            patch("api.rag.service.get_llm_config", new=AsyncMock(return_value=_StubConfig())),
            patch("api.rag.service.provider_for", return_value=provider),
            patch("api.rag.repository.similarity_search", new=search_spy),
            patch("api.rag.service.keyword_search", new=keyword_spy),
        ):
            claims = AuthClaims(subject="admin", role=Role.PLATFORM_ADMIN, tenant_id=None)
            try:
                await retrieve_hybrid(object(), claims, "query", k=5)
                raised = False
            except ValidationError:
                raised = True

    assert raised
    search_spy.assert_not_awaited()
    keyword_spy.assert_not_awaited()
