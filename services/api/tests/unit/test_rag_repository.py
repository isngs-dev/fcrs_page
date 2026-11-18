"""Unit tests for api.rag.repository.

Covers:
- search_chunks calls common.pgvector.similarity_search with
  table="approved_knowledge_chunks" (plan 2.6), the tenant claims, and the embedding as a bound
  (positional) arg -- never interpolated into SQL.
- distance -> score mapping: score = 1 - distance (e.g. distance 0.1 -> 0.9).
- Result order is preserved from the DB (descending score = ascending distance).
- Global caller (tenant_id=None, PLATFORM_ADMIN) -> ValidationError, and
  similarity_search is never called (no un-filtered query escapes).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.rag.repository import (
    ChunkMatch,
    KeywordMatch,
    keyword_search,
    resolve_chunks,
    search_chunks,
)

_TENANT_ID = "tenant-abc"
_OTHER_TENANT_ID = "tenant-xyz"


def _claims(tenant_id: str | None = _TENANT_ID, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


async def test_search_chunks_maps_distance_to_score_and_preserves_order() -> None:
    """distance 0.1 -> score 0.9; distance 0.4 -> score 0.6; DB order preserved."""
    rows = [
        {"doc_id": "doc-1", "chunk_id": "doc-1-0000", "content": "hello", "distance": 0.1,
         "category": "faq"},
        {"doc_id": "doc-1", "chunk_id": "doc-1-0001", "content": "world", "distance": 0.4,
         "category": "general"},
    ]
    stub = AsyncMock(return_value=rows)
    claims = _claims()
    embedding = [0.1, 0.2, 0.3]

    with patch("api.rag.repository.similarity_search", new=stub):
        result = await search_chunks(object(), claims, embedding, top_k=5)

    assert isinstance(result[0], ChunkMatch)
    assert [m.score for m in result] == pytest.approx([0.9, 0.6])
    assert [m.doc_id for m in result] == ["doc-1", "doc-1"]
    assert [m.chunk_id for m in result] == ["doc-1-0000", "doc-1-0001"]
    assert [m.content for m in result] == ["hello", "world"]


async def test_search_chunks_passes_table_claims_and_embedding_through() -> None:
    """The wrapper adds no un-filtered query -- it delegates tenant filtering and
    passes the query embedding as a bound argument, never string-interpolated."""
    stub = AsyncMock(return_value=[])
    claims = _claims()
    embedding = [0.5, 0.6]

    with patch("api.rag.repository.similarity_search", new=stub):
        await search_chunks(object(), claims, embedding, top_k=7)

    stub.assert_awaited_once()
    args, kwargs = stub.await_args
    assert args[1] == "approved_knowledge_chunks"  # plan 2.6: only approved documents
    assert args[2] is claims
    assert args[3] == embedding
    assert kwargs["top_k"] == 7
    assert kwargs["select"] == "doc_id, chunk_id, content, category"  # plan 2.6


async def test_search_chunks_global_caller_raises_validation_error() -> None:
    """PLATFORM_ADMIN (tenant_id=None) -> ValidationError; similarity_search not called."""
    stub = AsyncMock(return_value=[])
    claims = _claims(tenant_id=None, role=Role.PLATFORM_ADMIN)

    with patch("api.rag.repository.similarity_search", new=stub):
        with pytest.raises(ValidationError):
            await search_chunks(object(), claims, [0.1], top_k=5)

    stub.assert_not_awaited()


async def test_search_chunks_empty_result_returns_empty_list() -> None:
    stub = AsyncMock(return_value=[])
    claims = _claims()

    with patch("api.rag.repository.similarity_search", new=stub):
        result = await search_chunks(object(), claims, [0.1], top_k=5)

    assert result == []


class _StubKeywordDb:
    """Captures the SQL + bound args passed to ``fetch``."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.calls.append((query, args))
        return self._rows


async def test_keyword_search_binds_regconfig_query_and_limit_as_params() -> None:
    """The regconfig, query text, and LIMIT are ALL bound params -- never
    string-interpolated into the SQL text."""
    db = _StubKeywordDb(
        [
            {"doc_id": "doc-1", "chunk_id": "doc-1-0000", "content": "mystery shopping", "rank": 0.8,
             "category": "general"},
        ]
    )
    claims = _claims()

    result = await keyword_search(db, claims, "mystery shopping", top_k=7)

    assert len(db.calls) == 1
    sql, args = db.calls[0]
    # regconfig cast is bound, never interpolated as a literal string.
    assert "$1::regconfig" in sql
    assert "'english'" not in sql
    assert "mystery shopping" not in sql
    assert args[0] == "english"
    assert args[1] == "mystery shopping"
    assert 7 in args
    assert isinstance(result[0], KeywordMatch)
    assert result[0].rank == pytest.approx(0.8)


async def test_keyword_search_tenant_filter_fragment_present() -> None:
    db = _StubKeywordDb([])
    claims = _claims()

    await keyword_search(db, claims, "anything", top_k=5)

    sql, args = db.calls[0]
    assert "tenant_id" in sql
    assert claims.tenant_id in args


async def test_keyword_search_global_caller_raises_validation_error() -> None:
    db = _StubKeywordDb([])
    claims = _claims(tenant_id=None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await keyword_search(db, claims, "anything", top_k=5)

    assert db.calls == []


async def test_keyword_search_maps_rank_and_preserves_order() -> None:
    rows = [
        {"doc_id": "doc-1", "chunk_id": "c1", "content": "first", "rank": 0.9, "category": "pricing"},
        {"doc_id": "doc-1", "chunk_id": "c2", "content": "second", "rank": 0.4, "category": "faq"},
    ]
    db = _StubKeywordDb(rows)
    claims = _claims()

    result = await keyword_search(db, claims, "q", top_k=5)

    assert [m.rank for m in result] == pytest.approx([0.9, 0.4])
    assert [m.chunk_id for m in result] == ["c1", "c2"]
    assert [m.doc_id for m in result] == ["doc-1", "doc-1"]
    assert [m.content for m in result] == ["first", "second"]


async def test_keyword_search_empty_result_returns_empty_list() -> None:
    db = _StubKeywordDb([])
    claims = _claims()

    result = await keyword_search(db, claims, "nothing matches", top_k=5)

    assert result == []


# -- resolve_chunks (SR-2) -------------------------------------------------------
#
# resolve_chunks(db, claims, chunk_ids) resolves a message's historical
# `sources` chunk_ids to their LIVE knowledge_chunks.content, tenant-scoped
# INSIDE the query (decision 4, the sprint's load-bearing guarantee) via
# common.tenancy.tenant_filter -- never a post-fetch check.


class _StubResolveDb:
    """Captures the SQL + bound args passed to ``fetch`` for resolve_chunks."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.calls.append((query, args))
        return self._rows


async def test_resolve_chunks_binds_chunk_ids_array_and_tenant_filter() -> None:
    """MANDATORY tenant scoping: the query's WHERE includes tenant_filter's
    fragment (`AND tenant_id = $2`) and the caller's tenant_id is bound as a
    param; chunk_ids are bound as the $1 array via `chunk_id = ANY($1::text[])`
    -- never string-interpolated into the SQL text."""
    db = _StubResolveDb([{"chunk_id": "c1", "content": "Real chunk text."}])
    claims = _claims()

    result = await resolve_chunks(db, claims, ["c1"])

    assert result == {"c1": "Real chunk text."}
    assert len(db.calls) == 1
    sql, args = db.calls[0]
    assert "chunk_id = ANY($1::text[])" in sql
    assert "tenant_id = $2" in sql
    assert args[0] == ["c1"]
    assert args[1] == _TENANT_ID
    # Never interpolated: no chunk_id literal appears inline in the SQL text.
    assert "c1" not in sql


async def test_resolve_chunks_cross_tenant_id_cannot_resolve() -> None:
    """MANDATORY isolation: a chunk_id that (per a stub DB keyed on tenant)
    only "exists" for tenant B is absent from the map when resolved with
    tenant-A claims -- the tenant predicate excludes it at the SQL level, not
    via a post-fetch filter. Asserts the bound tenant param is A's."""

    class _TenantScopedStubDb:
        """Simulates real DB behavior: only returns rows matching the bound
        tenant_id param, mirroring what a real `tenant_id = $N` predicate
        would enforce."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []
            # tenant B "owns" chunk "shared-id" with real content.
            self._rows_by_tenant = {
                _OTHER_TENANT_ID: [{"chunk_id": "shared-id", "content": "Tenant B's secret content."}],
            }

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            self.calls.append((query, args))
            bound_tenant = args[1]
            return self._rows_by_tenant.get(str(bound_tenant), [])

    db = _TenantScopedStubDb()
    claims_a = _claims(tenant_id=_TENANT_ID)

    result = await resolve_chunks(db, claims_a, ["shared-id"])

    assert result == {}
    assert "shared-id" not in result
    # The bound tenant param is tenant A's -- never B's, never omitted.
    _, args = db.calls[0]
    assert args[1] == _TENANT_ID


async def test_resolve_chunks_global_caller_raises_and_issues_no_query() -> None:
    """MANDATORY: PLATFORM_ADMIN (tenant_id=None) -> ValidationError
    GLOBAL_CALLER_NOT_PERMITTED; no query issued."""
    db = _StubResolveDb([])
    claims = _claims(tenant_id=None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await resolve_chunks(db, claims, ["c1"])

    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
    assert db.calls == []


async def test_resolve_chunks_empty_list_returns_empty_dict_no_query() -> None:
    """chunk_ids=[] -> {} with no query issued (short-circuit)."""
    db = _StubResolveDb([])
    claims = _claims()

    result = await resolve_chunks(db, claims, [])

    assert result == {}
    assert db.calls == []


async def test_resolve_chunks_partial_resolution() -> None:
    """Three chunk_ids requested, stub returns rows for only two -> the map
    has exactly those two keys; the third is absent (drives the route's
    content:null/resolved:false marker)."""
    db = _StubResolveDb(
        [
            {"chunk_id": "c1", "content": "First chunk."},
            {"chunk_id": "c2", "content": "Second chunk."},
        ]
    )
    claims = _claims()

    result = await resolve_chunks(db, claims, ["c1", "c2", "c3-deleted"])

    assert result == {"c1": "First chunk.", "c2": "Second chunk."}
    assert "c3-deleted" not in result
