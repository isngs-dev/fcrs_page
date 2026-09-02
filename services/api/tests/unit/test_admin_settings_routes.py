"""Unit tests for GET/PUT /admin/settings (S12.2).

Covers:
- CLIENT_ADMIN GET/PUT both 200; CLIENT_AGENT GET 200, PUT 403.
- VISITOR/no-cookie -> 401/403.
- PUT persists + a follow-up GET reflects it (round-trip).
- PUT does NOT alter tenant_orchestrator_configs/tenant_llm_configs (assert
  via the stub DB that no UPDATE touches those tables).
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": _TEST_JWT_SECRET,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
    "ORCHESTRATOR_DEFAULT_ANSWER_THRESHOLD": "0.75",
    "ORCHESTRATOR_DEFAULT_ESCALATE_THRESHOLD": "0.25",
    "ORCHESTRATOR_DEFAULT_TURN_CAP": "6",
}


class _StubDatabase:
    """In-memory stub database backing /admin/settings for these tests."""

    def __init__(self) -> None:
        self._bot_settings: dict[str, dict[str, Any]] = {}
        self._orchestrator_configs: dict[str, dict[str, Any]] = {}
        self._llm_configs: dict[str, dict[str, Any]] = {}
        self.updated_tables: list[str] = []

    def seed_orchestrator_config(
        self,
        *,
        tenant_id: str,
        answer_threshold: float,
        escalate_threshold: float,
        turn_cap: int,
        identity_gate_enabled: bool | None = None,
        low_confidence_streak_cap: int | None = None,
    ) -> None:
        self._orchestrator_configs[tenant_id] = {
            "answer_threshold": answer_threshold,
            "escalate_threshold": escalate_threshold,
            "turn_cap": turn_cap,
            "identity_gate_enabled": identity_gate_enabled,
            "low_confidence_streak_cap": low_confidence_streak_cap,
        }

    def seed_llm_config(self, *, tenant_id: str, provider: str, model: str) -> None:
        self._llm_configs[tenant_id] = {"provider": provider, "model": model}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANT_BOT_SETTINGS" in q:
            tenant_id = args[0]
            return self._bot_settings.get(tenant_id)
        if "FROM TENANT_ORCHESTRATOR_CONFIGS" in q:
            tenant_id = args[0]
            return self._orchestrator_configs.get(tenant_id)
        if "FROM TENANT_LLM_CONFIGS" in q:
            tenant_id = args[0]
            return self._llm_configs.get(tenant_id)
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO TENANT_BOT_SETTINGS"):
            (
                tenant_id,
                greeting,
                business_hours,
                escalation_policy,
                tone,
                launcher_label,
                sidebar_workspace_label,
                dashboard_title,
                bot_name,
                accent_color,
                launcher_position,
                suggested_questions,
            ) = args
            self._bot_settings[tenant_id] = {
                "greeting": greeting,
                "business_hours": business_hours,
                "escalation_policy": escalation_policy,
                "tone": tone,
                "launcher_label": launcher_label,
                "sidebar_workspace_label": sidebar_workspace_label,
                "dashboard_title": dashboard_title,
                "bot_name": bot_name,
                "accent_color": accent_color,
                "launcher_position": launcher_position,
                "suggested_questions": suggested_questions,
            }
            self.updated_tables.append("tenant_bot_settings")
            return "INSERT 0 1"
        if "TENANT_ORCHESTRATOR_CONFIGS" in q and ("INSERT INTO" in q or q.startswith("UPDATE")):
            # Mirrors config_repository.upsert_orchestrator_config's real
            # INSERT ... ON CONFLICT DO UPDATE bind order.
            (
                tenant_id,
                answer_threshold,
                escalate_threshold,
                turn_cap,
                identity_gate_enabled,
                low_confidence_streak_cap,
            ) = args
            self._orchestrator_configs[tenant_id] = {
                "answer_threshold": answer_threshold,
                "escalate_threshold": escalate_threshold,
                "turn_cap": turn_cap,
                "identity_gate_enabled": identity_gate_enabled,
                "low_confidence_streak_cap": low_confidence_streak_cap,
            }
            self.updated_tables.append("tenant_orchestrator_configs")
            return "INSERT 0 1"
        if "TENANT_LLM_CONFIGS" in q and q.startswith("UPDATE"):
            self.updated_tables.append("tenant_llm_configs")
            return "UPDATE 1"
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
# GET /admin/settings
# ---------------------------------------------------------------------------


async def test_get_settings_client_admin_200(app: Any, db: _StubDatabase) -> None:
    db.seed_orchestrator_config(
        tenant_id=_TENANT_ID,
        answer_threshold=0.8,
        escalate_threshold=0.3,
        turn_cap=6,
        low_confidence_streak_cap=4,
    )
    db.seed_llm_config(tenant_id=_TENANT_ID, provider="anthropic", model="claude-sonnet")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/settings", cookies={"access_token": token})

    assert response.status_code == 200
    body = response.json()
    assert body["answer_threshold"] == 0.8
    assert body["escalate_threshold"] == 0.3
    assert body["turn_cap"] == 6
    assert body["low_confidence_streak_cap"] == 4
    assert body["llm_provider"] == "anthropic"
    assert body["llm_model"] == "claude-sonnet"
    assert body["greeting"] is None


async def test_get_settings_client_agent_200(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/settings", cookies={"access_token": token})

    assert response.status_code == 200


async def test_get_settings_visitor_forbidden(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/settings", cookies={"access_token": token})

    assert response.status_code == 403


async def test_get_settings_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/settings")

    assert response.status_code == 401


async def test_get_settings_never_includes_llm_api_key(app: Any, db: _StubDatabase) -> None:
    db.seed_llm_config(tenant_id=_TENANT_ID, provider="openai", model="gpt-4")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/settings", cookies={"access_token": token})

    body = response.json()
    assert "api_key" not in body
    assert "llm_api_key" not in body


# ---------------------------------------------------------------------------
# PUT /admin/settings
# ---------------------------------------------------------------------------


async def test_put_settings_client_admin_200_round_trip(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        put_response = await client.put(
            "/admin/settings",
            json={
                "greeting": "Welcome!",
                "business_hours": {"mon": "9-5"},
                "escalation_policy": "Escalate after 3 misses.",
                "tone": "friendly",
                "launcher_label": "Chat with our team",
                "sidebar_workspace_label": "Acme support",
                "dashboard_title": "Support hub",
            },
            cookies={"access_token": token},
        )
        get_response = await client.get("/admin/settings", cookies={"access_token": token})

    assert put_response.status_code == 200
    put_body = put_response.json()
    assert put_body["greeting"] == "Welcome!"
    assert put_body["launcher_label"] == "Chat with our team"
    assert put_body["sidebar_workspace_label"] == "Acme support"
    assert put_body["dashboard_title"] == "Support hub"

    get_body = get_response.json()
    assert get_body["greeting"] == "Welcome!"
    assert get_body["business_hours"] == {"mon": "9-5"}
    assert get_body["escalation_policy"] == "Escalate after 3 misses."
    assert get_body["tone"] == "friendly"
    assert get_body["launcher_label"] == "Chat with our team"
    assert get_body["sidebar_workspace_label"] == "Acme support"
    assert get_body["dashboard_title"] == "Support hub"


async def test_put_settings_branding_fields_round_trip(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        put_response = await client.put(
            "/admin/settings",
            json={
                "bot_name": "Aria",
                "accent_color": "#16a34a",
                "launcher_position": "left",
                "suggested_questions": ["Do you serve my area?", "How much does it cost?"],
            },
            cookies={"access_token": token},
        )
        get_response = await client.get("/admin/settings", cookies={"access_token": token})

    assert put_response.status_code == 200
    put_body = put_response.json()
    assert put_body["bot_name"] == "Aria"
    assert put_body["accent_color"] == "#16a34a"
    assert put_body["launcher_position"] == "left"
    assert put_body["suggested_questions"] == ["Do you serve my area?", "How much does it cost?"]

    get_body = get_response.json()
    assert get_body["bot_name"] == "Aria"
    assert get_body["accent_color"] == "#16a34a"
    assert get_body["launcher_position"] == "left"
    assert get_body["suggested_questions"] == ["Do you serve my area?", "How much does it cost?"]


async def test_branding_fields_are_tenant_isolated(app: Any, db: _StubDatabase) -> None:
    """Tenant A's bot_name/accent_color/launcher_position/suggested_questions
    write must never leak into tenant B's GET (mandatory multi-tenant
    isolation coverage, CLAUDE.md Sec.3)."""
    tenant_b = "tenant-b-456"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tenant_a_token = _token(Role.CLIENT_ADMIN, _TENANT_ID)
        tenant_b_token = _token(Role.CLIENT_ADMIN, tenant_b)
        write_response = await client.put(
            "/admin/settings",
            json={
                "bot_name": "Aria",
                "accent_color": "#16a34a",
                "launcher_position": "left",
                "suggested_questions": ["Do you serve my area?"],
            },
            cookies={"access_token": tenant_a_token},
        )
        tenant_b_response = await client.get(
            "/admin/settings", cookies={"access_token": tenant_b_token}
        )

    assert write_response.status_code == 200
    assert tenant_b_response.status_code == 200
    tenant_b_body = tenant_b_response.json()
    assert tenant_b_body["bot_name"] is None
    assert tenant_b_body["accent_color"] is None
    assert tenant_b_body["launcher_position"] is None
    assert tenant_b_body["suggested_questions"] is None
    # Bound to tenant A's own key in the stub DB -- never tenant B's.
    assert db._bot_settings[_TENANT_ID]["bot_name"] == "Aria"
    assert tenant_b not in db._bot_settings or db._bot_settings[tenant_b].get("bot_name") is None


async def test_put_settings_bad_accent_color_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"accent_color": "not-a-color"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_put_settings_bad_launcher_position_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"launcher_position": "center"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_put_settings_bot_name_too_long_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"bot_name": "x" * 41},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_put_settings_too_many_suggested_questions_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"suggested_questions": ["one", "two", "three", "four", "five", "six"]},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_put_settings_blank_suggested_question_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"suggested_questions": ["  "]},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_workspace_labels_are_tenant_isolated_and_empty_values_round_trip_as_null(
    app: Any, db: _StubDatabase
) -> None:
    tenant_b = "tenant-b-456"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tenant_a_token = _token(Role.CLIENT_ADMIN, _TENANT_ID)
        tenant_b_token = _token(Role.CLIENT_ADMIN, tenant_b)
        write_response = await client.put(
            "/admin/settings",
            json={
                "sidebar_workspace_label": "Acme support",
                "dashboard_title": "Support hub",
            },
            cookies={"access_token": tenant_a_token},
        )
        tenant_b_response = await client.get(
            "/admin/settings", cookies={"access_token": tenant_b_token}
        )
        empty_response = await client.put(
            "/admin/settings",
            json={"sidebar_workspace_label": "", "dashboard_title": ""},
            cookies={"access_token": tenant_a_token},
        )

    assert write_response.status_code == 200
    assert tenant_b_response.status_code == 200
    assert tenant_b_response.json()["sidebar_workspace_label"] is None
    assert tenant_b_response.json()["dashboard_title"] is None
    assert empty_response.json()["sidebar_workspace_label"] is None
    assert empty_response.json()["dashboard_title"] is None


async def test_put_settings_client_agent_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/settings",
            json={"greeting": "Hi", "launcher_label": "Agent cannot set this"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_put_settings_visitor_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.put(
            "/admin/settings",
            json={"greeting": "Hi", "launcher_label": "Visitor cannot set this"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_put_settings_no_cookie_401(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/admin/settings", json={"greeting": "Hi"})

    assert response.status_code == 401


async def test_put_settings_does_not_alter_orchestrator_or_llm_tables(
    app: Any, db: _StubDatabase
) -> None:
    db.seed_orchestrator_config(
        tenant_id=_TENANT_ID, answer_threshold=0.8, escalate_threshold=0.3, turn_cap=6
    )
    db.seed_llm_config(tenant_id=_TENANT_ID, provider="anthropic", model="claude-sonnet")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.put(
            "/admin/settings",
            json={"greeting": "Welcome!"},
            cookies={"access_token": token},
        )

    assert "tenant_orchestrator_configs" not in db.updated_tables
    assert "tenant_llm_configs" not in db.updated_tables
    assert "tenant_bot_settings" in db.updated_tables

    # Unchanged threshold/provider values still reflected on GET.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        get_response = await client.get("/admin/settings", cookies={"access_token": token})

    body = get_response.json()
    assert body["answer_threshold"] == 0.8
    assert body["llm_provider"] == "anthropic"


async def test_put_settings_greeting_too_long_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"greeting": "x" * 2001},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_put_settings_launcher_label_too_long_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"launcher_label": "x" * 41},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["sidebar_workspace_label", "dashboard_title"])
async def test_put_settings_workspace_label_too_long_422(app: Any, field: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={field: "x" * 81},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# PUT /admin/settings -- turn_cap (Tier 2)
# ---------------------------------------------------------------------------


async def test_put_settings_turn_cap_updates_orchestrator_config_preserving_thresholds(
    app: Any, db: _StubDatabase
) -> None:
    """Providing turn_cap writes tenant_orchestrator_configs, but the
    existing answer_threshold/escalate_threshold/identity_gate_enabled must
    survive untouched (the upsert is a full-row write)."""
    db.seed_orchestrator_config(
        tenant_id=_TENANT_ID,
        answer_threshold=0.8,
        escalate_threshold=0.3,
        turn_cap=6,
        identity_gate_enabled=True,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        put_response = await client.put(
            "/admin/settings",
            json={"greeting": "Hi", "turn_cap": 3},
            cookies={"access_token": token},
        )

    assert put_response.status_code == 200
    assert put_response.json()["turn_cap"] == 3
    assert "tenant_orchestrator_configs" in db.updated_tables

    stored = db._orchestrator_configs[_TENANT_ID]
    assert stored["turn_cap"] == 3
    assert stored["answer_threshold"] == 0.8
    assert stored["escalate_threshold"] == 0.3
    assert stored["identity_gate_enabled"] is True


async def test_put_settings_without_turn_cap_does_not_touch_orchestrator_config(
    app: Any, db: _StubDatabase
) -> None:
    """A PUT that omits turn_cap entirely (the pre-existing qualitative-only
    save) must leave tenant_orchestrator_configs completely untouched."""
    db.seed_orchestrator_config(
        tenant_id=_TENANT_ID, answer_threshold=0.8, escalate_threshold=0.3, turn_cap=6
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.put(
            "/admin/settings",
            json={"greeting": "Hi"},
            cookies={"access_token": token},
        )

    assert "tenant_orchestrator_configs" not in db.updated_tables
    assert db._orchestrator_configs[_TENANT_ID]["turn_cap"] == 6


async def test_put_settings_turn_cap_below_one_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"turn_cap": 0},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_put_settings_turn_cap_client_agent_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/settings",
            json={"turn_cap": 3},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# PUT /admin/settings -- low_confidence_streak_cap
# ---------------------------------------------------------------------------


async def test_put_settings_low_confidence_streak_cap_updates_preserving_rest(
    app: Any, db: _StubDatabase
) -> None:
    """Providing low_confidence_streak_cap writes tenant_orchestrator_configs,
    but turn_cap/thresholds/identity_gate_enabled must survive untouched."""
    db.seed_orchestrator_config(
        tenant_id=_TENANT_ID,
        answer_threshold=0.8,
        escalate_threshold=0.3,
        turn_cap=6,
        identity_gate_enabled=True,
        low_confidence_streak_cap=3,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        put_response = await client.put(
            "/admin/settings",
            json={"low_confidence_streak_cap": 2},
            cookies={"access_token": token},
        )

    assert put_response.status_code == 200
    assert put_response.json()["low_confidence_streak_cap"] == 2
    assert "tenant_orchestrator_configs" in db.updated_tables

    stored = db._orchestrator_configs[_TENANT_ID]
    assert stored["low_confidence_streak_cap"] == 2
    assert stored["turn_cap"] == 6
    assert stored["answer_threshold"] == 0.8
    assert stored["escalate_threshold"] == 0.3
    assert stored["identity_gate_enabled"] is True


async def test_put_settings_without_low_confidence_streak_cap_does_not_touch_orchestrator_config(
    app: Any, db: _StubDatabase
) -> None:
    db.seed_orchestrator_config(
        tenant_id=_TENANT_ID,
        answer_threshold=0.8,
        escalate_threshold=0.3,
        turn_cap=6,
        low_confidence_streak_cap=3,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.put(
            "/admin/settings",
            json={"greeting": "Hi"},
            cookies={"access_token": token},
        )

    assert "tenant_orchestrator_configs" not in db.updated_tables
    assert db._orchestrator_configs[_TENANT_ID]["low_confidence_streak_cap"] == 3


async def test_put_settings_turn_cap_alone_preserves_existing_low_confidence_streak_cap(
    app: Any, db: _StubDatabase
) -> None:
    """Providing ONLY turn_cap must not clobber the tenant's own
    low_confidence_streak_cap back to a default -- each cap is independently
    optional in the request body."""
    db.seed_orchestrator_config(
        tenant_id=_TENANT_ID,
        answer_threshold=0.8,
        escalate_threshold=0.3,
        turn_cap=6,
        low_confidence_streak_cap=2,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        put_response = await client.put(
            "/admin/settings",
            json={"turn_cap": 9},
            cookies={"access_token": token},
        )

    assert put_response.status_code == 200
    stored = db._orchestrator_configs[_TENANT_ID]
    assert stored["turn_cap"] == 9
    assert stored["low_confidence_streak_cap"] == 2


async def test_put_settings_low_confidence_streak_cap_below_one_422(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.put(
            "/admin/settings",
            json={"low_confidence_streak_cap": 0},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_put_settings_low_confidence_streak_cap_client_agent_403(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.put(
            "/admin/settings",
            json={"low_confidence_streak_cap": 2},
            cookies={"access_token": token},
        )

    assert response.status_code == 403
