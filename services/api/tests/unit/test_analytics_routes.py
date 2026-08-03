"""Unit tests for analytics routes (S11.2).

Covers:
- Happy path (CLIENT_ADMIN / CLIENT_AGENT) -> 200 nested shape, no PII leak.
- RBAC negatives: VISITOR -> 403, no cookie -> 401, PLATFORM_ADMIN -> 403.
- Window validation: from>=to -> 422, span too large -> 422, omitted -> default.
- Bucket validation: bucket=month -> 422, bucket=week -> 200.
- None rates serialize as JSON null (not 0.0).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"


class _StubDatabase:
    """Database double returning canned analytics rows (or empty -> all zero/None)."""

    def __init__(
        self,
        *,
        message_facts_rows: list[dict[str, Any]] | None = None,
        conversation_totals_row: dict[str, Any] | None = None,
        schedule_row: dict[str, Any] | None = None,
    ) -> None:
        self._message_facts_rows = message_facts_rows or []
        self._conversation_totals_row = conversation_totals_row or {"total": 0, "escalated": 0}
        self._schedule_row = schedule_row or {"cta_total": 0, "converted": 0}

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "GROUP BY role, decision, grounded, intent" in query:
            return self._message_facts_rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "cta_convs" in query:
            return self._schedule_row
        if "FROM conversations c" in query:
            return self._conversation_totals_row
        return None

    async def execute(self, query: str, *args: Any) -> str:
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


def _build_app(db: Any = None) -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db if db is not None else _StubDatabase()
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _mint_cookie(
    *,
    subject: str = "user-1",
    role: Role = Role.CLIENT_ADMIN,
    tenant_id: str | None = _TENANT_ID,
    ttl_seconds: int = 300,
    secret: str = _TEST_JWT_SECRET,
) -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=secret, ttl_seconds=ttl_seconds)
    return token


# ==============================================================================
# GET /admin/analytics/overview -- happy path
# ==============================================================================


async def test_client_admin_gets_200_full_nested_shape() -> None:
    db = _StubDatabase(
        message_facts_rows=[
            {"role": "bot", "decision": "answer", "grounded": True, "intent": "pricing", "cnt": 3},
        ],
        conversation_totals_row={"total": 2, "escalated": 0},
    )
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview", cookies={"access_token": token})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "window", "totals", "intent_distribution", "decision_distribution",
        "fallback_rate", "deflection_rate", "grounded_rate", "schedule", "series",
    }
    assert set(body["window"].keys()) == {"from", "to", "bucket"}
    assert set(body["totals"].keys()) == {
        "conversations", "user_turns", "bot_turns", "decided_bot_turns",
    }
    assert set(body["schedule"].keys()) == {"cta_conversations", "conversions", "conversion_rate"}
    assert body["intent_distribution"] == {"pricing": 3}


async def test_client_agent_allowed_200() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview", cookies={"access_token": token})
    assert resp.status_code == 200


async def test_response_contains_no_pii_or_internal_ids() -> None:
    """No tenant_id/visitor_id/conversation_id/message_id/message text anywhere in the body."""
    db = _StubDatabase(
        message_facts_rows=[
            {"role": "bot", "decision": "answer", "grounded": True, "intent": "pricing", "cnt": 3},
        ],
        conversation_totals_row={"total": 2, "escalated": 0},
        schedule_row={"cta_total": 1, "converted": 1},
    )
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview", cookies={"access_token": token})

    body_text = resp.text
    for leaked in ("tenant_id", "visitor_id", "conversation_id", "message_id", _TENANT_ID):
        assert leaked not in body_text


# ==============================================================================
# RBAC negatives
# ==============================================================================


async def test_visitor_returns_403() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.VISITOR)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview", cookies={"access_token": token})
    assert resp.status_code == 403


async def test_no_cookie_returns_401() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview")
    assert resp.status_code == 401


async def test_platform_admin_returns_403_role_not_permitted() -> None:
    """A PLATFORM_ADMIN/global cookie -> 403 (not in the allowed role set);
    the repository is never reached (require_roles rejects at the dependency)."""
    app = _build_app()
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview", cookies={"access_token": token})
    assert resp.status_code == 403


# ==============================================================================
# Window validation
# ==============================================================================


async def test_from_after_to_returns_422_invalid_window() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/overview"
            "?from=2026-07-13T00:00:00Z&to=2026-07-01T00:00:00Z",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_ANALYTICS_WINDOW"


async def test_from_equal_to_returns_422_invalid_window() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/overview"
            "?from=2026-07-01T00:00:00Z&to=2026-07-01T00:00:00Z",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_ANALYTICS_WINDOW"


async def test_span_exceeding_max_window_days_returns_422() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/overview"
            "?from=2020-01-01T00:00:00Z&to=2026-07-01T00:00:00Z",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "ANALYTICS_WINDOW_TOO_LARGE"


async def test_omitted_window_defaults_to_last_30_days() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview", cookies={"access_token": token})

    assert resp.status_code == 200
    body = resp.json()
    window_from = datetime.fromisoformat(body["window"]["from"].replace("Z", "+00:00"))
    window_to = datetime.fromisoformat(body["window"]["to"].replace("Z", "+00:00"))
    span = window_to - window_from
    assert abs(span - timedelta(days=30)) < timedelta(seconds=5)


# ==============================================================================
# Bucket validation
# ==============================================================================


async def test_bucket_hour_returns_422_invalid_bucket() -> None:
    """SR-9.5 D3: "hour" remains rejected (explicitly out of scope)."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/overview?bucket=hour",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_BUCKET"


async def test_bucket_month_returns_200_sr95_d3_widened_contract() -> None:
    """SR-9.5 D3: the shipped `overview` endpoint gains the `month` bucket
    too -- this returned 422 before this sprint; the explicit regression
    test proving the widened contract works live."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/overview?bucket=month",
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.json()["window"]["bucket"] == "month"


async def test_bucket_week_returns_200() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/overview?bucket=week",
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.json()["window"]["bucket"] == "week"


# ==============================================================================
# None rates serialize as JSON null
# ==============================================================================


async def test_empty_window_rates_are_json_null_not_zero() -> None:
    db = _StubDatabase()  # empty -> zero counts, all rates None
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/overview", cookies={"access_token": token})

    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback_rate"] is None
    assert body["deflection_rate"] is None
    assert body["grounded_rate"] is None
    assert body["schedule"]["conversion_rate"] is None
    # And it's JSON null (not the string "0.0"/number 0.0) in the raw text.
    assert '"fallback_rate":null' in resp.text.replace(" ", "") or '"fallback_rate": null' in resp.text
