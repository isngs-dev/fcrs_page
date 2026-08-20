"""Unit tests for api.admin.settings_repository (S12.2) -- unified bot-settings
read (decision 5) + qualitative-only write (decision 6).

``get_orchestrator_config`` is stubbed via monkeypatching the imported name in
``api.admin.settings_repository`` (not the orchestrator module itself --
that module is read-only per the sprint's constraints).
"""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.admin import settings_repository
from api.admin.settings_repository import get_bot_settings, upsert_bot_settings
from api.orchestrator.config_repository import OrchestratorConfig

_TENANT_A = "tenant-a-123"

_CLIENT_ADMIN = AuthClaims(subject="ca-1", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
_CLIENT_AGENT = AuthClaims(subject="cg-1", role=Role.CLIENT_AGENT, tenant_id=_TENANT_A)
_PLATFORM_ADMIN = AuthClaims(subject="pa-1", role=Role.PLATFORM_ADMIN, tenant_id=None)

_STUB_ORCH_CONFIG = OrchestratorConfig(
    answer_threshold=0.8, escalate_threshold=0.3, turn_cap=6, low_confidence_streak_cap=4
)


class _Call:
    def __init__(self, kind: str, query: str, params: tuple[Any, ...]) -> None:
        self.kind = kind
        self.query = query
        self.params = params


class _RecordingDB:
    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.fetchrow_returns: list[dict[str, Any] | None] = []
        self._fetchrow_i = 0

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(_Call("execute", query, args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(_Call("fetchrow", query, args))
        if self._fetchrow_i < len(self.fetchrow_returns):
            row = self.fetchrow_returns[self._fetchrow_i]
            self._fetchrow_i += 1
            return row
        self._fetchrow_i += 1
        return None


@pytest.fixture(autouse=True)
def _stub_orchestrator_config(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_orchestrator_config(db: Any, claims: AuthClaims) -> OrchestratorConfig:
        return _STUB_ORCH_CONFIG

    monkeypatch.setattr(
        settings_repository, "get_orchestrator_config", _fake_get_orchestrator_config
    )


# -- get_bot_settings ----------------------------------------------------------


async def test_get_bot_settings_merges_row_orchestrator_and_llm_config() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [
        {
            "greeting": "Hi there!",
            "business_hours": {"mon": "9-5"},
            "escalation_policy": "Escalate after 3 failed answers.",
            "tone": "friendly",
            "launcher_label": "Chat with our team",
            "sidebar_workspace_label": "Acme support",
            "dashboard_title": "Support hub",
        },
        {"provider": "anthropic", "model": "claude-sonnet"},
    ]

    result = await get_bot_settings(db, _CLIENT_ADMIN)

    assert result.greeting == "Hi there!"
    assert result.business_hours == {"mon": "9-5"}
    assert result.escalation_policy == "Escalate after 3 failed answers."
    assert result.tone == "friendly"
    assert result.launcher_label == "Chat with our team"
    assert result.sidebar_workspace_label == "Acme support"
    assert result.dashboard_title == "Support hub"
    assert result.answer_threshold == 0.8
    assert result.escalate_threshold == 0.3
    assert result.turn_cap == 6
    assert result.low_confidence_streak_cap == 4
    assert result.llm_provider == "anthropic"
    assert result.llm_model == "claude-sonnet"


async def test_get_bot_settings_no_row_yet_qualitative_fields_none_thresholds_populated() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [None, None]

    result = await get_bot_settings(db, _CLIENT_ADMIN)

    assert result.greeting is None
    assert result.business_hours is None
    assert result.escalation_policy is None
    assert result.tone is None
    assert result.launcher_label is None
    assert result.sidebar_workspace_label is None
    assert result.dashboard_title is None
    assert result.answer_threshold == 0.8
    assert result.escalate_threshold == 0.3
    assert result.turn_cap == 6
    assert result.low_confidence_streak_cap == 4
    assert result.llm_provider is None
    assert result.llm_model is None


async def test_get_bot_settings_llm_query_never_selects_api_key_column() -> None:
    db = _RecordingDB()
    db.fetchrow_returns = [None, None]

    await get_bot_settings(db, _CLIENT_ADMIN)

    llm_call = db.calls[-1]
    assert "tenant_llm_configs" in llm_call.query
    assert "api_key" not in llm_call.query.lower()


async def test_get_bot_settings_rejects_global_caller() -> None:
    db = _RecordingDB()

    with pytest.raises(ValidationError) as exc_info:
        await get_bot_settings(db, _PLATFORM_ADMIN)

    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
    assert db.calls == []


# -- upsert_bot_settings ---------------------------------------------------------


async def test_upsert_bot_settings_binds_on_conflict_upsert_qualitative_only() -> None:
    db = _RecordingDB()

    await upsert_bot_settings(
        db,
        _CLIENT_ADMIN,
        greeting="Hello!",
        business_hours={"mon": "9-5"},
        escalation_policy="Escalate always.",
        tone="formal",
        launcher_label="Chat with our team",
        sidebar_workspace_label="Acme support",
        dashboard_title="Support hub",
    )

    assert len(db.calls) == 1
    query = db.calls[0].query
    assert "ON CONFLICT (tenant_id) DO UPDATE" in query
    assert "tenant_bot_settings" in query

    params = db.calls[0].params
    assert _TENANT_A in params
    assert "Hello!" in params
    assert "Escalate always." in params
    assert "formal" in params
    assert "Chat with our team" in params
    assert "Acme support" in params
    assert "Support hub" in params

    # No threshold/provider param anywhere in the captured SQL/params.
    assert "answer_threshold" not in query
    assert "escalate_threshold" not in query
    assert "turn_cap" not in query
    assert "provider" not in query
    assert "tenant_orchestrator_configs" not in query
    assert "tenant_llm_configs" not in query


async def test_upsert_bot_settings_rejects_global_caller() -> None:
    db = _RecordingDB()

    with pytest.raises(ValidationError) as exc_info:
        await upsert_bot_settings(
            db,
            _PLATFORM_ADMIN,
            greeting=None,
            business_hours=None,
            escalation_policy=None,
            tone=None,
            launcher_label=None,
            sidebar_workspace_label=None,
            dashboard_title=None,
        )

    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
    assert db.calls == []
