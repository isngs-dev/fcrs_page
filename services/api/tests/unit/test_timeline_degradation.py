"""Caching x degradation interplay for the SR-9.3 timeline (D8).

These tests specifically exercise the cache-aside wrapping in
``timeline/admin_routes.py`` against a REAL ``InMemoryCache`` (not the
no-op stub used by the route-level RBAC/attribution suite), verifying:
- Two identical requests issue the source queries once (cache hit second
  time).
- The cache key contains the tenant id (`tenant:<tenant_id>:` prefix).
- Tenant A's cache is never served to tenant B for the same contact_id
  string.
- A degraded response is NEVER cached -- the very next call after the
  source recovers returns complete, fresh data.
- Different before/limit produce different cache entries.
- TTL expiry triggers a requery.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-cache-a"
_OTHER_TENANT_ID = "tenant-cache-b"

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

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _StubDatabase:
    """Minimal stub: one contact, no identities -- keeps every source's
    fetch cheap so we can count query invocations precisely via patching."""

    def __init__(self) -> None:
        self._tenants: dict[str, dict[str, Any]] = {}
        self._contacts: dict[tuple[str, str], dict[str, Any]] = {}

    def seed_tenant(self, *, tenant_id: str, slug: str) -> None:
        self._tenants[tenant_id] = {"id": tenant_id, "name": slug, "slug": slug, "enabled": True}

    def seed_contact(self, *, tenant_id: str, contact_id: str) -> None:
        self._contacts[(tenant_id, contact_id)] = {
            "tenant_id": tenant_id, "contact_id": contact_id, "account_id": None,
            "lead_id": None, "name": "Dana", "email": "dana@example.com", "phone": None,
            "consent": {"granted": True}, "owner_agent_id": None,
            "created_at": _NOW, "updated_at": _NOW,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANTS WHERE ID" in q:
            return self._tenants.get(args[0])
        if "FROM CONTACTS" in q and "CONTACT_ID = $2" in q:
            tenant_id, contact_id = args[0], args[1]
            return self._contacts.get((tenant_id, contact_id))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    async def close(self) -> None:
        pass


class _StubRedis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        pass

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_app(db: _StubDatabase, cache: InMemoryCache) -> Any:
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
    app.state.cache = cache
    return app


def _token(role: Role, tenant_id: str | None = _TENANT_ID) -> str:
    claims = AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=_TEST_JWT_SECRET, ttl_seconds=300)
    return token


async def _get(app: Any, path: str, tenant_id: str = _TENANT_ID) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, tenant_id=tenant_id)
        return await client.get(path, cookies={"access_token": token})


async def test_two_identical_requests_query_sources_once() -> None:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    cache = InMemoryCache()
    app = _build_app(d, cache)

    with patch(
        "api.timeline.service._fetch_lead_activities", return_value=[],
    ) as mock_fetch:
        r1 = await _get(app, "/admin/contacts/contact-1/timeline")
        r2 = await _get(app, "/admin/contacts/contact-1/timeline")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    mock_fetch.assert_called_once()


async def test_cache_key_contains_tenant_id_prefix() -> None:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    cache = InMemoryCache()
    app = _build_app(d, cache)

    await _get(app, "/admin/contacts/contact-1/timeline")

    assert any(k.startswith(f"tenant:{_TENANT_ID}:timeline:") for k in cache._store)


async def test_tenant_a_cache_never_served_to_tenant_b_same_contact_id() -> None:
    """Same contact_id string exists in both tenants; each tenant's cached
    entry must be isolated by the tenant-scoped key."""
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_tenant(tenant_id=_OTHER_TENANT_ID, slug="widgetco")
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-shared")
    d.seed_contact(tenant_id=_OTHER_TENANT_ID, contact_id="contact-shared")
    cache = InMemoryCache()
    app = _build_app(d, cache)

    r_a = await _get(app, "/admin/contacts/contact-shared/timeline", tenant_id=_TENANT_ID)
    r_b = await _get(app, "/admin/contacts/contact-shared/timeline", tenant_id=_OTHER_TENANT_ID)

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    keys = list(cache._store.keys())
    assert any(k.startswith(f"tenant:{_TENANT_ID}:timeline:") for k in keys)
    assert any(k.startswith(f"tenant:{_OTHER_TENANT_ID}:timeline:") for k in keys)


async def test_degraded_response_not_cached_next_call_fresh() -> None:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    cache = InMemoryCache()
    app = _build_app(d, cache)

    with patch(
        "api.timeline.service._fetch_lead_activities",
        side_effect=RuntimeError("db unavailable"),
    ):
        r1 = await _get(app, "/admin/contacts/contact-1/timeline")

    assert r1.status_code == 200
    assert r1.json()["degraded"] is True
    # Nothing was cached for a degraded response.
    assert cache._store == {}

    with patch(
        "api.timeline.service._fetch_lead_activities", return_value=[],
    ):
        r2 = await _get(app, "/admin/contacts/contact-1/timeline")

    assert r2.status_code == 200
    assert r2.json()["degraded"] is False


async def test_different_before_and_limit_produce_different_cache_entries() -> None:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")
    cache = InMemoryCache()
    app = _build_app(d, cache)

    await _get(app, "/admin/contacts/contact-1/timeline?limit=10")
    await _get(app, "/admin/contacts/contact-1/timeline?limit=20")
    await _get(app, "/admin/contacts/contact-1/timeline?before=2026-01-01T00:00:00Z")

    assert len(cache._store) == 3


async def test_ttl_expiry_triggers_requery() -> None:
    d = _StubDatabase()
    d.seed_tenant(tenant_id=_TENANT_ID, slug="acme")
    d.seed_contact(tenant_id=_TENANT_ID, contact_id="contact-1")

    fake_time = [1000.0]
    cache = InMemoryCache(time_fn=lambda: fake_time[0])
    app = _build_app(d, cache)

    with patch(
        "api.timeline.service._fetch_lead_activities", return_value=[],
    ) as mock_fetch:
        await _get(app, "/admin/contacts/contact-1/timeline")
        assert mock_fetch.call_count == 1

        # Within TTL (default 60s): still cached.
        fake_time[0] += 30
        await _get(app, "/admin/contacts/contact-1/timeline")
        assert mock_fetch.call_count == 1

        # Past TTL: requery.
        fake_time[0] += 40
        await _get(app, "/admin/contacts/contact-1/timeline")
        assert mock_fetch.call_count == 2
