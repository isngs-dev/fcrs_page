"""Unit tests for api.leads.tasks.classify_lead_email (Celery task "leads.classify_lead_email").

Covers:
- Definitive verdict (qualified/disqualified) from captured -> stage moves,
  verdict persisted, a system-actor "stage_change" activity appended.
- needs_review verdict -> verdict persisted, stage untouched.
- Missing lead -> no-op success, no writes.
- NULL email (anonymous booking lead) -> no-op success, classify_email never
  called, no writes.
- Lead already moved off "captured" by the time this runs -> verdict still
  persisted, but the stage move is skipped (never clobbers a human decision).
- Multi-tenant isolation: a task for tenant A never reads/writes tenant B's
  lead even if the same lead_id string were reused.
- correlation_id declared on the task signature (S5.1 regression guard).
- Tenant-scoped AuthClaims built from the trusted tenant_id kwarg.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

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

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_TENANT_ID = "tenant-leads-tasks-test"
_LEAD_ID = "lead-leads-tasks-test"


def _reset_modules() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("api.leads") or key.startswith("api.tasks"):
            del sys.modules[key]
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


class _RecordingDatabase:
    """Minimal DB double for classify_lead_email: one lead row + write recording."""

    def __init__(self, *, lead_row: dict[str, Any] | None) -> None:
        self._lead_row = dict(lead_row) if lead_row is not None else None
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.upper()
        if "FROM LEADS" in q:
            if self._lead_row is None:
                return None
            if self._lead_row.get("tenant_id", args[0]) != args[0]:
                return None
            return self._lead_row
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.executions.append((query, args))
        q = query.strip().upper()
        if q.startswith("UPDATE LEADS SET EMAIL_VERDICT"):
            verdict, reason, tenant_id, lead_id = args
            if self._lead_row is not None:
                self._lead_row["email_verdict"] = verdict
                self._lead_row["email_verdict_reason"] = reason
            return "UPDATE 1"
        if q.startswith("UPDATE LEADS SET STAGE"):
            stage, status, score, tenant_id, lead_id = args
            if self._lead_row is not None:
                self._lead_row["stage"] = stage
                self._lead_row["status"] = status
                self._lead_row["qualification_score"] = score
            return "UPDATE 1"
        return "INSERT 1"

    async def close(self) -> None:
        pass


def _make_lead_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": _TENANT_ID,
        "lead_id": _LEAD_ID,
        "visitor_id": "visitor-1",
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": None,
        "status": "new",
        "stage": "captured",
        "qualification_score": None,
        "consent": {"granted": True, "purpose": "contact", "text": "OK"},
        "assigned_agent_id": None,
        "source": "widget",
        "created_at": _NOW,
        "updated_at": _NOW,
        "converted_to_contact_id": None,
        "email_verdict": None,
        "email_verdict_reason": None,
    }
    row.update(overrides)
    return row


def _activity_inserts(db: _RecordingDatabase) -> list[tuple[str, tuple[Any, ...]]]:
    return [e for e in db.executions if "INSERT INTO LEAD_ACTIVITIES" in e[0].upper()]


def _verdict_updates(db: _RecordingDatabase) -> list[tuple[str, tuple[Any, ...]]]:
    return [e for e in db.executions if e[0].strip().upper().startswith("UPDATE LEADS SET EMAIL_VERDICT")]


def _stage_updates(db: _RecordingDatabase) -> list[tuple[str, tuple[Any, ...]]]:
    return [e for e in db.executions if e[0].strip().upper().startswith("UPDATE LEADS SET STAGE")]


# ==============================================================================
# Definitive verdict moves stage
# ==============================================================================


async def test_qualified_verdict_moves_captured_to_qualified() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        db = _RecordingDatabase(lead_row=_make_lead_row(email="jane@example.com"))

        with patch("api.leads.tasks.check_mx_cached", new=AsyncMock(return_value="ok")):
            from api.leads.tasks import _execute  # noqa: PLC0415

            result = await _execute(db, _TENANT_ID, _LEAD_ID)  # type: ignore[arg-type]

    assert result["status"] == "succeeded"
    assert result["verdict"] == "qualified"
    assert result["stage_moved"] is True

    v_updates = _verdict_updates(db)
    assert len(v_updates) == 1
    assert v_updates[0][1][0] == "qualified"
    assert v_updates[0][1][1] == "ok"

    s_updates = _stage_updates(db)
    assert len(s_updates) == 1
    assert s_updates[0][1][0] == "qualified"

    activities = _activity_inserts(db)
    assert len(activities) == 1
    _, args = activities[0]
    # args: tenant_id, activity_id, lead_id, type, payload, actor
    assert args[3] == "stage_change"
    assert args[4] == {"from_stage": "captured", "to_stage": "qualified", "reason": "ok"}
    assert args[5] == "system:lead-qualification"


async def test_disqualified_verdict_moves_captured_to_disqualified() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        db = _RecordingDatabase(lead_row=_make_lead_row(email="jane@mailinator.com"))

        from api.leads.tasks import _execute  # noqa: PLC0415

        result = await _execute(db, _TENANT_ID, _LEAD_ID)  # type: ignore[arg-type]

    assert result["verdict"] == "disqualified"
    assert result["stage_moved"] is True
    s_updates = _stage_updates(db)
    assert s_updates[0][1][0] == "disqualified"


# ==============================================================================
# needs_review: verdict persisted, stage untouched
# ==============================================================================


async def test_needs_review_verdict_persists_but_never_moves_stage() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        db = _RecordingDatabase(lead_row=_make_lead_row(email="test@example.com"))

        with patch("api.leads.tasks.check_mx_cached", new=AsyncMock(return_value="ok")):
            from api.leads.tasks import _execute  # noqa: PLC0415

            result = await _execute(db, _TENANT_ID, _LEAD_ID)  # type: ignore[arg-type]

    assert result["verdict"] == "needs_review"
    assert result["stage_moved"] is False
    assert len(_verdict_updates(db)) == 1
    assert _stage_updates(db) == []
    assert _activity_inserts(db) == []


# ==============================================================================
# Missing lead / NULL email -> no-op
# ==============================================================================


async def test_missing_lead_is_no_op_no_writes() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        db = _RecordingDatabase(lead_row=None)

        from api.leads.tasks import _execute  # noqa: PLC0415

        result = await _execute(db, _TENANT_ID, _LEAD_ID)  # type: ignore[arg-type]

    assert result["status"] == "no_op"
    assert db.executions == []


async def test_null_email_is_no_op_never_calls_classify() -> None:
    """SR-9.1 anonymous booking-created lead: email IS NULL -- must be
    skipped entirely, never disqualified for 'having no email'."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        db = _RecordingDatabase(lead_row=_make_lead_row(email=None, name=None))

        with patch("api.leads.tasks.classify_email") as mock_classify:
            from api.leads.tasks import _execute  # noqa: PLC0415

            result = await _execute(db, _TENANT_ID, _LEAD_ID)  # type: ignore[arg-type]

    assert result["status"] == "no_op"
    assert result["verdict"] is None
    mock_classify.assert_not_called()
    assert db.executions == []


# ==============================================================================
# Lead already moved on -> verdict persisted, stage move skipped
# ==============================================================================


async def test_lead_already_moved_off_captured_skips_stage_move_but_persists_verdict() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        # An agent already manually advanced this lead to "contacted" before
        # this task ran.
        db = _RecordingDatabase(lead_row=_make_lead_row(email="jane@example.com", stage="contacted"))

        with patch("api.leads.tasks.check_mx_cached", new=AsyncMock(return_value="ok")):
            from api.leads.tasks import _execute  # noqa: PLC0415

            result = await _execute(db, _TENANT_ID, _LEAD_ID)  # type: ignore[arg-type]

    assert result["verdict"] == "qualified"
    assert result["stage_moved"] is False
    assert len(_verdict_updates(db)) == 1  # verdict still recorded
    assert _stage_updates(db) == []  # but stage was never touched
    assert _activity_inserts(db) == []


# ==============================================================================
# correlation_id declared on the task (S5.1 regression guard)
# ==============================================================================


def test_classify_lead_email_delay_accepts_correlation_id() -> None:
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        import api.leads.tasks  # noqa: PLC0415, F401
        import api.tasks.celery_app as capp  # noqa: PLC0415

        capp.celery_app.conf.task_always_eager = True
        capp.celery_app.conf.task_eager_propagates = False

        from api.leads.tasks import classify_lead_email  # noqa: PLC0415

        with patch("api.leads.tasks.asyncio.new_event_loop") as mock_loop:
            mock_event_loop = mock_loop.return_value
            mock_event_loop.run_until_complete.return_value = {
                "lead_id": _LEAD_ID, "status": "no_op", "verdict": None, "stage_moved": False,
            }
            mock_event_loop.close.return_value = None

            result = classify_lead_email.delay(
                tenant_id=_TENANT_ID,
                lead_id=_LEAD_ID,
                correlation_id="cid-leads-test",
            )
            assert result is not None


# ==============================================================================
# Tenant-scoped AuthClaims
# ==============================================================================


async def test_builds_tenant_scoped_claims() -> None:
    _reset_modules()
    captured_claims: list[Any] = []

    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.config import get_api_settings  # noqa: PLC0415

        get_api_settings.cache_clear()
        db = _RecordingDatabase(lead_row=None)

        from api.leads import tasks as tasks_mod  # noqa: PLC0415
        from api.leads.repository import get_lead as original_get_lead  # noqa: PLC0415

        async def _capturing_get_lead(db_: Any, claims_: Any, lead_id_: Any) -> Any:
            captured_claims.append(claims_)
            return await original_get_lead(db_, claims_, lead_id_)

        with patch("api.leads.tasks.get_lead", side_effect=_capturing_get_lead):
            result = await tasks_mod._execute(db, _TENANT_ID, _LEAD_ID)  # type: ignore[arg-type]

    assert result["status"] == "no_op"
    assert captured_claims, "get_lead should have been called"

    from common.auth import AuthClaims as _AC  # noqa: PLC0415
    from common.auth import Role as _R  # noqa: PLC0415

    c = captured_claims[0]
    assert isinstance(c, _AC)
    assert c.subject == "system:lead-qualification"
    assert c.role == _R.CLIENT_ADMIN
    assert c.tenant_id == _TENANT_ID
