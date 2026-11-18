"""Plan 2.6 on a REAL migrated Postgres: knowledge documents carry a category and an
"approved" switch; only approved documents are retrieved (chatbot and calls alike); admins
set both, audited; agents can't; another client can't touch them."""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.db import Database
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token
from api.ingestion import repository as repo
from api.rag import repository as rag_repo

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)

_JWT = "x" * 48
_ENV = {
    "DEPLOYMENT_MODE": "saas", "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379", "JWT_SECRET": _JWT,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api", "LOG_LEVEL": "WARNING", "COOKIE_SECURE": "false",
}
_VEC = [0.1] * 768


class _Redis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_api_settings.cache_clear()
    assert _TEST_DB_URL is not None
    from common.pgvector import register_vector_init

    db = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0,
                                init=register_vector_init)  # like the app
    tenants = []
    for _ in range(2):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, $1, $1, true, $1)", tid)
        tenants.append(tid)
    from api.app import create_app

    app = create_app()
    app.state.db, app.state.redis, app.state.cache = db, _Redis(), InMemoryCache()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"db": db, "client": client, "a": tenants[0], "b": tenants[1]}
    await db.close()
    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims(tenant: str) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id=tenant)


def _cookie(tenant: str | None, role: Role = Role.CLIENT_ADMIN) -> dict[str, str]:
    token, _ = create_access_token(AuthClaims(subject="admin-1", role=role, tenant_id=tenant),
                                   secret=_JWT, ttl_seconds=300)
    return {"access_token": token}


async def _doc(db: Database, tenant: str, text: str, *, category: str | None = None) -> str:
    """A ready document with one chunk of ``text``."""
    kwargs: dict[str, Any] = {"category": category} if category else {}
    doc = await repo.create_doc(db, _claims(tenant), source="upload", filename="f.txt",
                                content_type="text/plain", content_hash=uuid4().hex,
                                storage_key=uuid4().hex, **kwargs)
    await db.execute("INSERT INTO knowledge_chunks (tenant_id, doc_id, chunk_id, content, "
                     "embedding) VALUES ($1, $2, $3, $4, $5)", tenant, doc.doc_id,
                     uuid4().hex, text, _VEC)
    return doc.doc_id


async def test_a_new_document_is_general_and_approved_until_told_otherwise(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    doc_id = await _doc(db, a, "We install metal roofs.")
    doc = await repo.get_doc(db, _claims(a), doc_id)
    assert doc is not None and (doc.category, doc.approved) == ("general", True)
    priced = await _doc(db, a, "Inspections cost $200.", category="pricing")
    assert (await repo.get_doc(db, _claims(a), priced)).category == "pricing"  # type: ignore[union-attr]


async def test_only_approved_documents_are_retrieved_with_their_category(
        env: dict[str, Any]) -> None:
    db, a = env["db"], env["a"]
    kept = await _doc(db, a, "Roof inspections are free in Miami.", category="faq")
    pulled = await _doc(db, a, "Roof inspections are free everywhere, forever.")
    await repo.set_doc_review(db, _claims(a), pulled, approved=False, reviewed_by="admin-1")
    words = await rag_repo.keyword_search(db, _claims(a), "roof inspections free", top_k=10)
    assert [(m.doc_id, m.category) for m in words] == [(kept, "faq")]
    near = await rag_repo.search_chunks(db, _claims(a), _VEC, top_k=10)
    assert [(m.doc_id, m.category) for m in near] == [(kept, "faq")]


async def test_admins_set_category_and_approval_audited_agents_and_others_cannot(
        env: dict[str, Any]) -> None:
    db, client, a, b = env["db"], env["client"], env["a"], env["b"]
    doc_id = await _doc(db, a, "Our prices: inspection $200.")
    url = f"/admin/ingestion/docs/{doc_id}"
    r = await client.patch(url, json={"category": "pricing", "approved": False},
                           cookies=_cookie(a))
    assert r.status_code == 200 and (r.json()["category"], r.json()["approved"]) == (
        "pricing", False)
    doc = await repo.get_doc(db, _claims(a), doc_id)
    assert doc is not None and doc.reviewed_by == "admin-1" and doc.reviewed_at is not None
    assert (await client.patch(url, json={"approved": True},
                               cookies=_cookie(a, Role.CLIENT_AGENT))).status_code == 403
    assert (await client.patch(url, json={"approved": True},
                               cookies=_cookie(b))).status_code == 404  # another client's doc
    assert (await client.patch(url, json={"category": "gossip"},
                               cookies=_cookie(a))).status_code == 422
    listed = (await client.get("/admin/ingestion/docs", cookies=_cookie(a))).json()["docs"]
    assert [(d["category"], d["approved"]) for d in listed] == [("pricing", False)]
    assert await db.fetchval("SELECT count(*) FROM audit_events WHERE tenant_id = $1 AND "
                             "action = 'knowledge.doc_reviewed'", a) == 1


async def test_an_upload_can_say_its_category(env: dict[str, Any],
                                              monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Any) -> None:
    from api.ingestion import routes

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    from api.config import get_api_settings

    get_api_settings.cache_clear()
    monkeypatch.setattr(routes, "ingest_document", MagicMock())
    r = await env["client"].post(
        "/admin/ingestion/upload", files={"file": ("prices.txt", b"Inspection $200", "text/plain")},
        data={"category": "pricing"}, cookies=_cookie(env["a"]))
    assert r.status_code == 200, r.text
    doc = await repo.get_doc(env["db"], _claims(env["a"]), r.json()["doc_id"])
    assert doc is not None and doc.category == "pricing" and doc.approved
