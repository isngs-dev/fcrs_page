"""FastAPI application factory.

Boot via ``uvicorn api.app:create_app --factory``.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from common.cache import build_cache
from common.db import Database
from common.errors import AppException, InternalServerError, RateLimitError
from common.health import check_database, check_redis, liveness, metrics_payload, readiness
from common.logging import get_logger, log_context
from common.pgvector import register_vector_init
from common.ratelimit import build_rate_limiter
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from api.config import ApiSettings, get_api_settings
from api.observability.sentry import capture_exception, init_sentry

_log = get_logger(__name__)


def _validate_runtime_config(settings: ApiSettings) -> None:
    """Fail fast on missing conditional infra config (CLAUDE.md §config)."""
    if settings.storage_backend.lower() == "local" and not settings.storage_local_root:
        raise RuntimeError(
            "STORAGE_BACKEND=local requires STORAGE_LOCAL_ROOT to be set. "
            "Set it in .env (repo root) or the environment. See deploy/.env.example."
        )
    if settings.storage_backend.lower() == "s3" and not settings.storage_s3_bucket:
        raise RuntimeError(
            "STORAGE_BACKEND=s3 requires STORAGE_S3_BUCKET to be set. "
            "Set it in .env (repo root) or the environment. See deploy/.env.example."
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Connect DB and optional Redis on startup; close on shutdown."""
    settings = get_api_settings()
    _validate_runtime_config(settings)

    # S6.1 decision 1: register the pgvector codec alongside the default jsonb
    # codec so app.state.db round-trips list[float] <-> vector (needed to bind
    # a query embedding as $1 for RAG search), not just dict <-> jsonb.
    db = await Database.connect(
        settings.database_url,
        statement_cache_size=0,
        init=register_vector_init,
    )
    app.state.db = db

    redis_client: Any = None
    if settings.redis_url:
        from redis.asyncio import from_url

        redis_client = from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        app.state.redis = redis_client

    app.state.rate_limiter = build_rate_limiter(redis_client)
    app.state.cache = build_cache(settings.redis_url)

    _log.info("api started", extra={"event": "startup"})
    try:
        yield
    finally:
        await db.close()
        if redis_client is not None:
            await redis_client.aclose()
        _log.info("api stopped", extra={"event": "shutdown"})


def _error_response(exc: AppException, correlation_id: str) -> JSONResponse:
    """Build a JSON error envelope from an AppException."""
    body: dict[str, Any] = {
        "error_code": exc.code,
        "message": exc.message,
        "correlation_id": correlation_id,
    }
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitError) and exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.http_status, content=body, headers=headers)


def create_app() -> FastAPI:
    """Application factory -- called by uvicorn ``--factory``."""
    # Fail-fast: settings validated here; missing/invalid env -> crash before serving.
    settings = get_api_settings()

    # Configure structured JSON logging.
    root = get_logger("api")
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # S11.3: optional Sentry init (no-op if DSN unset or sentry-sdk not installed).
    init_sentry(settings.sentry_dsn, settings.environment)

    app = FastAPI(title="Chatbot API", version="0.1.0", lifespan=_lifespan)

    # -- HTTP metrics middleware (S11.3) -- records request latency + counts ----
    from api.observability.middleware import metrics_middleware

    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next: Any) -> Response:
        return await metrics_middleware(request, call_next)

    # -- Edge middleware (security headers + CORS) -- outermost so it wraps
    #    everything including the correlation-id middleware and error handlers.
    @app.middleware("http")
    async def edge_middleware(request: Request, call_next: Any) -> Response:
        from api.edge import apply_cors_headers, apply_security_headers, is_known_origin

        origin = request.headers.get("origin")
        allowed = False
        if origin:
            db: Database = request.app.state.db
            cache = getattr(request.app.state, "cache", None)
            if cache is not None:
                allowed = await is_known_origin(
                    db, cache, origin, ttl=settings.cors_origin_cache_ttl_seconds
                )

        # Preflight short-circuit
        if request.method == "OPTIONS" and origin is not None:
            preflight_resp = Response(status_code=204)
            if allowed:
                apply_cors_headers(preflight_resp, origin, max_age=settings.cors_preflight_max_age)
            apply_security_headers(preflight_resp)
            return preflight_resp

        resp: Response = await call_next(request)

        if origin is not None and allowed:
            apply_cors_headers(resp, origin, max_age=settings.cors_preflight_max_age)
        apply_security_headers(resp)
        return resp

    # -- Correlation-ID middleware (also catches unhandled exceptions) ----------
    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next: Any) -> Response:
        cid = request.headers.get("x-correlation-id") or uuid4().hex
        with log_context(correlation_id=cid):
            try:
                response: Response = await call_next(request)
            except Exception as exc:
                _log.exception("unhandled exception", extra={"event": "unhandled_error"})
                capture_exception(exc)
                safe = InternalServerError()
                return JSONResponse(
                    status_code=safe.http_status,
                    content={
                        "error_code": safe.code,
                        "message": safe.message,
                        "correlation_id": cid,
                    },
                )
            response.headers["X-Correlation-Id"] = cid
            return response

    # -- Exception handler for AppException ------------------------------------
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        from common.logging import _correlation_id  # noqa: PLC2701

        cid = _correlation_id.get() or ""
        _log.warning(
            exc.message,
            extra={"status_code": exc.http_status, "event": "app_error"},
        )
        return _error_response(exc, cid)

    # -- Routers ---------------------------------------------------------------
    from api.accounts.admin_routes import router as accounts_admin_router
    from api.accounts.admin_routes import tenant_scoped_router as accounts_admin_tenant_router
    from api.admin.api_keys_routes import router as admin_api_keys_router
    from api.admin.api_keys_routes import (
        tenant_scoped_router as admin_api_keys_tenant_router,
    )
    from api.admin.assignment_routes import router as admin_assignment_router
    from api.admin.assignment_routes import (
        tenant_scoped_router as admin_assignment_tenant_router,
    )
    from api.admin.routes import router as admin_router
    from api.admin.settings_routes import router as admin_settings_router
    from api.admin.settings_routes import tenant_scoped_router as admin_settings_tenant_router
    from api.admin.users_routes import router as admin_users_router
    from api.admin.users_routes import tenant_scoped_router as admin_users_tenant_router
    from api.admin.workspace_routes import router as admin_workspace_router
    from api.admin.workspace_routes import (
        tenant_scoped_router as admin_workspace_tenant_router,
    )
    from api.analytics.reports_routes import router as analytics_reports_router
    from api.analytics.reports_routes import (
        tenant_scoped_router as analytics_reports_tenant_router,
    )
    from api.analytics.routes import router as analytics_router
    from api.analytics.routes import tenant_scoped_router as analytics_tenant_router
    from api.audit.routes import router as audit_router
    from api.audit.routes import tenant_scoped_router as audit_tenant_router
    from api.auth.routes import router as auth_router
    from api.contacts.admin_routes import router as contacts_admin_router
    from api.contacts.admin_routes import tenant_scoped_router as contacts_admin_tenant_router
    from api.conversation_store.admin_routes import router as conversation_admin_router
    from api.conversation_store.admin_routes import (
        tenant_scoped_router as conversation_admin_tenant_router,
    )
    from api.conversation_store.routes import router as conversation_router
    from api.crm.routes import router as crm_router
    from api.gateway.routes import router as gateway_router
    from api.ingestion.routes import router as ingestion_router
    from api.ingestion.routes import tenant_scoped_router as ingestion_tenant_router
    from api.leads.admin_routes import router as leads_admin_router
    from api.leads.admin_routes import tenant_scoped_router as leads_admin_tenant_router
    from api.leads.identity_routes import router as identity_router
    from api.leads.routes import router as leads_router
    from api.llm.routes import router as llm_router
    from api.notifications.admin_routes import router as notifications_admin_router
    from api.notifications.admin_routes import (
        tenant_scoped_router as notifications_admin_tenant_router,
    )
    from api.opportunities.admin_routes import router as opportunities_admin_router
    from api.opportunities.admin_routes import (
        tenant_scoped_router as opportunities_admin_tenant_router,
    )
    from api.orchestrator.routes import router as chat_router
    from api.rag.routes import router as rag_router
    from api.rbac.routes import router as rbac_router
    from api.scheduling.admin_routes import router as scheduling_admin_router
    from api.scheduling.calendly_webhook import router as calendly_webhook_router
    from api.scheduling.routes import router as scheduling_router
    from api.tasks.routes import router as tasks_router
    from api.tenants.routes import router as tenants_router
    from api.timeline.admin_routes import router as timeline_router
    from api.timeline.admin_routes import tenant_scoped_router as timeline_tenant_router

    app.include_router(accounts_admin_router)
    app.include_router(admin_api_keys_router)
    app.include_router(admin_assignment_router)
    app.include_router(admin_router)
    app.include_router(admin_settings_router)
    app.include_router(admin_users_router)
    app.include_router(admin_workspace_router)
    app.include_router(analytics_router)
    app.include_router(analytics_reports_router)
    app.include_router(audit_router)
    app.include_router(auth_router)
    app.include_router(contacts_admin_router)
    app.include_router(conversation_admin_router)
    app.include_router(conversation_router)
    app.include_router(crm_router)
    app.include_router(gateway_router)
    app.include_router(identity_router)
    app.include_router(ingestion_router)
    app.include_router(leads_admin_router)
    app.include_router(leads_router)
    app.include_router(llm_router)
    app.include_router(notifications_admin_router)
    app.include_router(opportunities_admin_router)
    app.include_router(chat_router)
    app.include_router(rag_router)
    app.include_router(rbac_router)
    app.include_router(scheduling_admin_router)
    app.include_router(scheduling_router)
    app.include_router(calendly_webhook_router)
    app.include_router(tasks_router)
    app.include_router(tenants_router)
    app.include_router(timeline_router)

    # -- Platform-admin tenant-explicit routers (S12.7) -------------------------
    # /admin/tenants/{tenant_id}/... -- same business logic as the routers
    # above, reached via resolve_tenant_scope instead of require_roles. The
    # implicit routers above are byte-for-byte unchanged for CLIENT_ADMIN/
    # CLIENT_AGENT.
    app.include_router(accounts_admin_tenant_router)
    app.include_router(admin_api_keys_tenant_router)
    app.include_router(admin_assignment_tenant_router)
    app.include_router(admin_settings_tenant_router)
    app.include_router(admin_users_tenant_router)
    app.include_router(admin_workspace_tenant_router)
    app.include_router(analytics_tenant_router)
    app.include_router(analytics_reports_tenant_router)
    app.include_router(audit_tenant_router)
    app.include_router(contacts_admin_tenant_router)
    app.include_router(conversation_admin_tenant_router)
    app.include_router(ingestion_tenant_router)
    app.include_router(leads_admin_tenant_router)
    app.include_router(notifications_admin_tenant_router)
    app.include_router(opportunities_admin_tenant_router)
    app.include_router(timeline_tenant_router)

    # -- Routes ----------------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return liveness()

    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        checks: dict[str, Any] = {
            "database": lambda: check_database(request.app.state.db),
        }
        if hasattr(request.app.state, "redis"):
            checks["redis"] = lambda: check_redis(request.app.state.redis)
        ready, detail = await readiness(checks)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "checks": detail},
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        body, content_type = metrics_payload()
        return Response(content=body, media_type=content_type)

    return app
