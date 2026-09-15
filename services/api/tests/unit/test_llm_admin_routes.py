"""Unit tests for GET/POST /admin/llm/config (AI provider settings feature).

Covers:
- GET: unconfigured tenant -> 200 all-null/false (never 404); configured ->
  200 with has_api_key/has_embedding_api_key, never any key material in the
  response body; RBAC (CLIENT_ADMIN only); no cookie -> 401.
- POST: first-ever config with no api_key -> 422 API_KEY_REQUIRED; first-ever
  config WITH api_key -> 200; an existing config with api_key omitted -> 200,
  has_api_key stays true (preserved, not cleared); invalid provider -> 422;
  RBAC; never logs/audits key material.
- Deliberately no PLATFORM_ADMIN tenant-scoped mirror route (S13.7 precedent) --
  confirmed genuinely absent (404), not just unauthorized.
- Isolation: tenant A's POST never affects tenant B's stored config.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-llm-abc-123"
_OTHER_TENANT_ID = "tenant-llm-xyz-999"

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": _TEST_JWT_SECRET,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


class _StubDatabase:
    """In-memory stub backing tenant_llm_configs + audit_events."""

    def __init__(self) -> None:
        self._configs: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    def seed_config(
        self,
        *,
        tenant_id: str,
        provider: str = "openai",
        model: str = "gpt-4o",
        has_api_key: bool = True,
        has_embedding_api_key: bool = False,
        embedding_model: str | None = None,
    ) -> None:
        self._configs[tenant_id] = {
            "tenant_id": tenant_id,
            "provider": provider,
            "model": model,
            "base_url": None,
            "api_version": None,
            "embedding_model": embedding_model,
            "embedding_base_url": None,
            "embedding_dimensions": None,
            "has_api_key": has_api_key,
            "has_embedding_api_key": has_embedding_api_key,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        tenant_id = args[0]
        if q.startswith("SELECT TENANT_ID FROM TENANT_LLM_CONFIGS"):
            # upsert_llm_config's existence check.
            return {"tenant_id": tenant_id} if tenant_id in self._configs else None
        if "FROM TENANT_LLM_CONFIGS" in q:
            # get_llm_config_summary's narrow read.
            row = self._configs.get(tenant_id)
            return dict(row) if row is not None else None
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO TENANT_LLM_CONFIGS"):
            (
                tenant_id,
                provider,
                model,
                ciphertext,
                base_url,
                api_version,
                embedding_model,
                embedding_base_url,
                embedding_ciphertext,
                embedding_dimensions,
            ) = args
            existing = self._configs.get(tenant_id, {})
            has_api_key = existing.get("has_api_key", False) if ciphertext is None else True
            has_embedding_api_key = (
                existing.get("has_embedding_api_key", False)
                if embedding_ciphertext is None
                else True
            )
            self._configs[tenant_id] = {
                "tenant_id": tenant_id,
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "api_version": api_version,
                "embedding_model": embedding_model,
                "embedding_base_url": embedding_base_url,
                "embedding_dimensions": embedding_dimensions,
                "has_api_key": has_api_key,
                "has_embedding_api_key": has_embedding_api_key,
            }
            return "INSERT 1"
        if q.startswith("INSERT INTO AUDIT_EVENTS"):
            (tenant_id, event_id, actor, action, target_type, target_id, metadata) = args
            self.audit_rows.append(
                {
                    "tenant_id": tenant_id,
                    "event_id": event_id,
                    "actor": actor,
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "metadata": metadata,
                }
            )
        return "OK"

    async def close(self) -> None:
        pass


class _StubRedis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        pass

    async def getdel(self, key: str) -> str | None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app(db: _StubDatabase) -> Any:
    _reset_settings()
    import os

    old_env = {k: os.environ.get(k) for k in _TEST_SETTINGS_ENV}
    os.environ.update(_TEST_SETTINGS_ENV)
    try:
        from api.app import create_app

        app = create_app()
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    app.state.db = db
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _token(role: Role, tenant_id: str | None = _TENANT_ID, subject: str = "admin-1") -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


@pytest.fixture
def db() -> _StubDatabase:
    return _StubDatabase()


@pytest.fixture
def app(db: _StubDatabase) -> Any:
    return _build_app(db)


# ---------------------------------------------------------------------------
# GET /admin/llm/config
# ---------------------------------------------------------------------------


async def test_get_llm_config_unconfigured_returns_200_all_null_false(app: Any) -> None:
    """Never a 404 for "not configured yet" -- an honest empty state."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/llm/config", cookies={"access_token": token})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] is None
    assert body["has_api_key"] is False
    assert body["has_embedding_api_key"] is False


async def test_get_llm_config_configured_returns_summary_never_key_material(
    app: Any, db: _StubDatabase
) -> None:
    db.seed_config(
        tenant_id=_TENANT_ID,
        provider="anthropic",
        model="claude-opus-4-8",
        has_api_key=True,
        has_embedding_api_key=True,
        embedding_model="text-embedding-3-small",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/llm/config", cookies={"access_token": token})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-opus-4-8"
    assert body["embedding_model"] == "text-embedding-3-small"
    assert body["has_api_key"] is True
    assert body["has_embedding_api_key"] is True
    assert "api_key" not in body
    assert "embedding_api_key" not in body
    assert "ciphertext" not in str(body).lower()


async def test_get_llm_config_client_agent_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/llm/config", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_llm_config_visitor_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/llm/config", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_llm_config_platform_admin_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/llm/config", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_llm_config_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/llm/config")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/llm/config
# ---------------------------------------------------------------------------


async def test_post_llm_config_first_time_requires_api_key_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/llm/config",
            json={"provider": "openai", "model": "gpt-4o"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "API_KEY_REQUIRED"


async def test_post_llm_config_first_time_with_api_key_succeeds(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/llm/config",
            json={"provider": "openai", "model": "gpt-4o", "api_key": "sk-real-key"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openai"
    assert body["has_api_key"] is True
    assert "sk-real-key" not in str(body)


async def test_post_llm_config_blank_api_key_on_update_preserves_has_key(
    app: Any, db: _StubDatabase
) -> None:
    """Editing the model without re-entering the key must not clear it."""
    db.seed_config(tenant_id=_TENANT_ID, provider="openai", model="gpt-4o", has_api_key=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/llm/config",
            json={"provider": "openai", "model": "gpt-4o-mini"},  # api_key omitted
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "gpt-4o-mini"
    assert body["has_api_key"] is True  # preserved, not cleared


async def test_post_llm_config_sets_embedding_model_for_ingestion(
    app: Any, db: _StubDatabase
) -> None:
    """The exact fix for EMBEDDING_NOT_CONFIGURED: setting embedding_model
    via this route makes it show up on a subsequent GET."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.post(
            "/admin/llm/config",
            json={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-real-key",
                "embedding_model": "text-embedding-3-small",
            },
            cookies={"access_token": token},
        )
        get_response = await client.get("/admin/llm/config", cookies={"access_token": token})

    assert get_response.json()["embedding_model"] == "text-embedding-3-small"


async def test_post_llm_config_invalid_provider_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/llm/config",
            json={"provider": "cohere", "model": "x", "api_key": "sk-key"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_post_llm_config_client_agent_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            "/admin/llm/config",
            json={"provider": "openai", "model": "gpt-4o", "api_key": "sk-key"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_post_llm_config_visitor_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post(
            "/admin/llm/config",
            json={"provider": "openai", "model": "gpt-4o", "api_key": "sk-key"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_post_llm_config_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/llm/config", json={"provider": "openai", "model": "gpt-4o", "api_key": "sk-key"}
        )

    assert response.status_code == 401


async def test_post_llm_config_never_logs_or_audits_key_material(
    app: Any, db: _StubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    raw_key = "sk-super-secret-value"
    with caplog.at_level(logging.INFO):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.post(
                "/admin/llm/config",
                json={"provider": "openai", "model": "gpt-4o", "api_key": raw_key},
                cookies={"access_token": token},
            )

    for record in caplog.records:
        assert raw_key not in record.getMessage()
        assert raw_key not in str(getattr(record, "__dict__", {}))
    for row in db.audit_rows:
        assert raw_key not in str(row)


# ---------------------------------------------------------------------------
# No PLATFORM_ADMIN tenant-scoped mirror (design decision 2)
# ---------------------------------------------------------------------------


async def test_no_tenant_scoped_mirror_route_exists(app: Any) -> None:
    """Unlike /admin/api-keys, there is deliberately no
    /admin/tenants/{tenant_id}/llm/config twin -- confirm it's genuinely
    absent (404), not just unauthorized."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/llm/config", cookies={"access_token": token}
        )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


async def test_post_llm_config_never_affects_another_tenant(app: Any, db: _StubDatabase) -> None:
    db.seed_config(tenant_id=_OTHER_TENANT_ID, provider="anthropic", model="claude-opus-4-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)
        await client.post(
            "/admin/llm/config",
            json={"provider": "openai", "model": "gpt-4o", "api_key": "sk-key"},
            cookies={"access_token": token},
        )

    other = db._configs[_OTHER_TENANT_ID]
    assert other["provider"] == "anthropic"
    assert other["model"] == "claude-opus-4-8"
