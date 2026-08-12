"""Unit tests for the API application shell.

Uses httpx.AsyncClient with ASGITransport -- no live DB or Redis needed.
Test doubles are injected via app.state after create_app().
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from common.cache import InMemoryCache
from common.errors import NotFoundError
from httpx import ASGITransport, AsyncClient

# -- Test doubles --------------------------------------------------------------


class _StubDatabase:
    """Database double whose fetchval can succeed or raise."""

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    async def fetchval(self, query: str, *args: object) -> object:
        if not self._healthy:
            raise RuntimeError("db down")
        return 1

    async def close(self) -> None:
        pass


class _StubRedis:
    """Stub redis.asyncio client."""

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    async def ping(self) -> bool:
        if not self._healthy:
            raise ConnectionError("redis down")
        return True

    async def aclose(self) -> None:
        pass


# -- Helpers -------------------------------------------------------------------

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "SENTRY_DSN": "",
    "ENVIRONMENT": "test",
}


def _build_app(
    *,
    db_healthy: bool = True,
    redis_healthy: bool = True,
    include_redis: bool = True,
    extra_routes: bool = False,
) -> Any:
    """Create app with test doubles injected, bypassing the real lifespan."""
    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from common.settings import get_settings

        from api.config import get_api_settings

        get_settings.cache_clear()
        get_api_settings.cache_clear()
        from api.app import create_app

        app = create_app()
        get_settings.cache_clear()
        get_api_settings.cache_clear()

    # Replace lifespan-provided state with test doubles
    app.state.db = _StubDatabase(healthy=db_healthy)
    if include_redis:
        app.state.redis = _StubRedis(healthy=redis_healthy)
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None

    if extra_routes:

        @app.get("/test-not-found")
        async def _raise_not_found() -> None:
            raise NotFoundError("thing not here")

        @app.get("/test-unhandled")
        async def _raise_unhandled() -> None:
            raise RuntimeError("kaboom internal")

    return app


# -- /healthz ------------------------------------------------------------------


async def test_healthz_returns_200() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# -- /readyz -------------------------------------------------------------------


async def test_readyz_ready_200() -> None:
    app = _build_app(db_healthy=True, redis_healthy=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


async def test_readyz_db_down_503() -> None:
    app = _build_app(db_healthy=False, redis_healthy=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["database"] == "fail"


async def test_readyz_redis_down_503() -> None:
    app = _build_app(db_healthy=True, redis_healthy=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["redis"] == "fail"


async def test_readyz_no_redis_configured() -> None:
    app = _build_app(db_healthy=True, include_redis=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert "redis" not in body["checks"]


# -- /metrics ------------------------------------------------------------------


async def test_metrics_returns_200_with_prometheus_content_type() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


# -- Correlation ID ------------------------------------------------------------


async def test_correlation_id_echoed() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/healthz", headers={"X-Correlation-Id": "test-123"})
    assert resp.headers["x-correlation-id"] == "test-123"


async def test_correlation_id_generated_when_absent() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/healthz")
    cid = resp.headers.get("x-correlation-id")
    assert cid is not None
    assert len(cid) == 32  # uuid4().hex is 32 hex chars


# -- Router registration (S10.1) -------------------------------------------------


async def test_chat_router_registered() -> None:
    """POST /public/chat/message route exists (chat_router is registered)."""
    app = _build_app()
    route_paths: set[str] = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is not None:
            route_paths.add(path)
        original_router = getattr(r, "original_router", None)
        if original_router is not None:
            for sub in original_router.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    route_paths.add(sub_path)
    assert "/public/chat/message" in route_paths


async def test_chat_stream_router_registered() -> None:
    """POST /public/chat/message/stream route exists (S10.5) alongside the
    unchanged POST /public/chat/message."""
    app = _build_app()
    route_paths: set[str] = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is not None:
            route_paths.add(path)
        original_router = getattr(r, "original_router", None)
        if original_router is not None:
            for sub in original_router.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    route_paths.add(sub_path)
    assert "/public/chat/message/stream" in route_paths
    assert "/public/chat/message" in route_paths


# -- Error envelope ------------------------------------------------------------


async def test_app_exception_returns_envelope() -> None:
    app = _build_app(extra_routes=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/test-not-found")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "NOT_FOUND"
    assert "message" in body
    assert "correlation_id" in body


async def test_unhandled_exception_returns_500_without_leak() -> None:
    app = _build_app(extra_routes=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/test-unhandled")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "kaboom" not in body["message"]
    assert "correlation_id" in body


async def test_unhandled_exception_records_500_metric_and_captures_sentry() -> None:
    """An endpoint that raises → 500 metric recorded AND capture_exception called."""
    from prometheus_client import REGISTRY, generate_latest

    app = _build_app(extra_routes=True)

    # Monkeypatch capture_exception to track calls. capture_exception is
    # SYNCHRONOUS (called un-awaited in the middleware), so the double must be
    # sync too -- an async double would return a never-awaited coroutine and
    # the append would never run.
    captured: list[Exception] = []

    def _fake_capture(exc: Exception) -> None:
        captured.append(exc)

    with patch("api.app.capture_exception", side_effect=_fake_capture):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/test-unhandled")

    assert resp.status_code == 500
    assert len(captured) == 1
    assert isinstance(captured[0], RuntimeError)

    # Verify the 500 metric was recorded
    output = generate_latest(REGISTRY).decode("utf-8")
    assert 'status="500"' in output


# -- _validate_runtime_config --------------------------------------------------


def _make_settings(**overrides: object) -> Any:
    """Return a minimal ApiSettings-like stub for _validate_runtime_config tests."""
    from unittest.mock import MagicMock

    stub = MagicMock()
    stub.storage_backend = overrides.get("storage_backend", "local")
    stub.storage_local_root = overrides.get("storage_local_root", None)
    # Explicit default (not left to MagicMock's auto-truthy attribute
    # fallback) so an s3-backend test that forgets to set this fails loudly
    # instead of silently passing the fail-fast check for the wrong reason.
    stub.storage_s3_bucket = overrides.get("storage_s3_bucket", None)
    return stub


def test_validate_runtime_config_raises_when_local_and_root_unset() -> None:
    """local backend + no STORAGE_LOCAL_ROOT → RuntimeError naming the var."""
    from api.app import _validate_runtime_config

    stub = _make_settings(storage_backend="local", storage_local_root=None)
    with pytest.raises(RuntimeError, match="STORAGE_LOCAL_ROOT"):
        _validate_runtime_config(stub)


def test_validate_runtime_config_passes_when_local_and_root_set() -> None:
    """local backend + root provided → no exception."""
    from api.app import _validate_runtime_config

    stub = _make_settings(storage_backend="local", storage_local_root="/some/path")
    _validate_runtime_config(stub)  # must not raise


def test_validate_runtime_config_s3_backend_ignores_local_root() -> None:
    """s3 backend + no STORAGE_LOCAL_ROOT → no exception (root is local-only)."""
    from api.app import _validate_runtime_config

    stub = _make_settings(
        storage_backend="s3", storage_local_root=None, storage_s3_bucket="my-bucket"
    )
    _validate_runtime_config(stub)  # must not raise


def test_validate_runtime_config_raises_when_s3_and_bucket_unset() -> None:
    """s3 backend + no STORAGE_S3_BUCKET → RuntimeError naming the var."""
    from api.app import _validate_runtime_config

    stub = _make_settings(storage_backend="s3", storage_s3_bucket=None)
    with pytest.raises(RuntimeError, match="STORAGE_S3_BUCKET"):
        _validate_runtime_config(stub)


def test_validate_runtime_config_passes_when_s3_and_bucket_set() -> None:
    """s3 backend + bucket provided → no exception."""
    from api.app import _validate_runtime_config

    stub = _make_settings(storage_backend="s3", storage_s3_bucket="my-bucket")
    _validate_runtime_config(stub)  # must not raise


# -- _lifespan: pgvector codec wiring (S6.1 decision 1) -------------------------


async def test_lifespan_database_connect_called_with_register_vector_init() -> None:
    """``_lifespan`` must connect the app DB with ``init=register_vector_init``.

    Without this, ``app.state.db`` only has the jsonb codec; binding a query
    embedding (``list[float]``) as ``$1`` for the RAG search endpoint fails at
    runtime against a live DB (the S5.2/S5.3 codec lesson, now at the HTTP
    layer -- S6.1 decision 1). Stub tests can't catch a missing codec directly,
    so this test instead asserts the wiring: ``Database.connect`` is invoked
    with the correct ``init`` callback.
    """
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import _lifespan, create_app

        app = create_app()
        get_settings.cache_clear()
        get_api_settings.cache_clear()

    connect_kwargs: list[dict[str, Any]] = []

    class _StubDb:
        async def close(self) -> None:
            pass

    async def _stub_connect(dsn: str, **kwargs: Any) -> Any:
        connect_kwargs.append(kwargs)
        return _StubDb()

    with patch("api.app.Database.connect", side_effect=_stub_connect):
        async with _lifespan(app):
            pass

    assert connect_kwargs, "Database.connect must have been called"

    from common.pgvector import register_vector_init

    assert connect_kwargs[0].get("init") is register_vector_init, (
        "app.state.db must be connected with init=register_vector_init "
        "(S6.1 decision 1 -- the app-db codec wiring)"
    )


# -- Router registration (S11.2) ------------------------------------------------


async def test_admin_onboard_tenant_router_registered() -> None:
    """POST /admin/tenants route exists (admin_router is registered, S12.1)."""
    app = _build_app()
    route_paths: set[str] = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is not None:
            route_paths.add(path)
        original_router = getattr(r, "original_router", None)
        if original_router is not None:
            for sub in original_router.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    route_paths.add(sub_path)
    assert "/admin/tenants" in route_paths


async def test_admin_rotate_key_router_registered() -> None:
    """POST /admin/tenants/{tenant_id}/rotate-key route exists (S12.1)."""
    app = _build_app()
    route_paths: set[str] = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is not None:
            route_paths.add(path)
        original_router = getattr(r, "original_router", None)
        if original_router is not None:
            for sub in original_router.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    route_paths.add(sub_path)
    assert "/admin/tenants/{tenant_id}/rotate-key" in route_paths


async def test_analytics_overview_router_registered() -> None:
    """GET /admin/analytics/overview route exists (analytics_router is registered)."""
    app = _build_app()
    route_paths: set[str] = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is not None:
            route_paths.add(path)
        original_router = getattr(r, "original_router", None)
        if original_router is not None:
            for sub in original_router.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    route_paths.add(sub_path)
    assert "/admin/analytics/overview" in route_paths


# -- Router registration (S12.2) ------------------------------------------------


def _all_route_paths(app: Any) -> set[str]:
    route_paths: set[str] = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is not None:
            route_paths.add(path)
        original_router = getattr(r, "original_router", None)
        if original_router is not None:
            for sub in original_router.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    route_paths.add(sub_path)
    return route_paths


async def test_admin_users_list_create_routes_registered() -> None:
    """GET/POST /admin/users routes exist (admin_users_router registered, S12.2)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert "/admin/users" in route_paths


async def test_admin_users_patch_route_registered() -> None:
    """PATCH /admin/users/{user_id} route exists (S12.2)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert "/admin/users/{user_id}" in route_paths


async def test_admin_settings_get_put_routes_registered() -> None:
    """GET/PUT /admin/settings routes exist (admin_settings_router registered, S12.2)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert "/admin/settings" in route_paths


# -- Router registration (S12.4) ------------------------------------------------


async def test_admin_leads_list_route_registered() -> None:
    """GET /admin/leads route exists (S12.4 -- new sibling on leads_admin_router)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert "/admin/leads" in route_paths


async def test_admin_conversations_list_route_registered() -> None:
    """GET /admin/conversations route exists (conversation_admin_router registered, S12.4)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert "/admin/conversations" in route_paths


async def test_admin_conversation_detail_route_registered() -> None:
    """GET /admin/conversations/{conversation_id} route exists (S12.4)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert "/admin/conversations/{conversation_id}" in route_paths


# -- Router registration (SR-2) --------------------------------------------------


async def test_admin_message_sources_route_registered() -> None:
    """GET /admin/conversations/{conversation_id}/messages/{message_id}/sources
    route exists (SR-2 grounding spot-check, implicit-tenant router)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert "/admin/conversations/{conversation_id}/messages/{message_id}/sources" in route_paths


# -- Router registration (SR-4) --------------------------------------------------


def _route_methods_by_path(app: Any) -> dict[str, set[str]]:
    methods_by_path: dict[str, set[str]] = {}

    def _collect(routes: Any) -> None:
        for r in routes:
            path = getattr(r, "path", None)
            methods = getattr(r, "methods", None)
            if path is not None and methods:
                methods_by_path.setdefault(path, set()).update(methods)
            original_router = getattr(r, "original_router", None)
            if original_router is not None:
                _collect(original_router.routes)

    _collect(app.routes)
    return methods_by_path


async def test_ingestion_delete_doc_route_registered() -> None:
    """DELETE /admin/ingestion/docs/{doc_id} route exists (SR-4)."""
    app = _build_app()
    methods_by_path = _route_methods_by_path(app)
    assert "DELETE" in methods_by_path.get("/admin/ingestion/docs/{doc_id}", set())


async def test_ingestion_delete_doc_tenant_scoped_route_registered() -> None:
    """DELETE /admin/tenants/{tenant_id}/ingestion/docs/{doc_id} route exists (SR-4,
    PLATFORM_ADMIN super-user variant)."""
    app = _build_app()
    methods_by_path = _route_methods_by_path(app)
    assert "DELETE" in methods_by_path.get(
        "/admin/tenants/{tenant_id}/ingestion/docs/{doc_id}", set()
    )


async def test_admin_message_sources_tenant_scoped_route_registered() -> None:
    """GET /admin/tenants/{tenant_id}/conversations/{conversation_id}/messages/
    {message_id}/sources route exists (SR-2, PLATFORM_ADMIN super-user variant)."""
    app = _build_app()
    route_paths = _all_route_paths(app)
    assert (
        "/admin/tenants/{tenant_id}/conversations/{conversation_id}/messages/{message_id}/sources"
        in route_paths
    )


async def test_calendly_webhook_route_registered() -> None:
    """POST /public/calendly/webhook/{tenant_id} route exists (SR-6)."""
    app = _build_app()
    methods_by_path = _route_methods_by_path(app)
    assert "POST" in methods_by_path.get("/public/calendly/webhook/{tenant_id}", set())


async def test_handoff_intent_route_registered() -> None:
    """POST /public/schedule/handoff-intent route exists (SR-6)."""
    app = _build_app()
    methods_by_path = _route_methods_by_path(app)
    assert "POST" in methods_by_path.get("/public/schedule/handoff-intent", set())
