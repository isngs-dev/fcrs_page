"""Unit tests for /admin/leads/{lead_id} (GET, PATCH).

Covers:
- GET returns 200 with stage/status/qualification_score for an existing lead.
- PATCH with a valid transition -> 200, new stage, derived status, recomputed
  qualification_score; nothing persisted for an illegal transition.
- PATCH with an illegal transition -> 422 INVALID_STAGE_TRANSITION.
- GET/PATCH of an unknown lead -> 404.
- RBAC: CLIENT_AGENT allowed, CLIENT_ADMIN allowed, VISITOR -> 403, no auth -> 401.
- Cross-tenant: agent of tenant B addressing tenant A's lead -> 404.
- PII (name/email/phone/consent text) is never logged on a transition.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"
_OTHER_TENANT_ID = "tenant-xyz-999"

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

_PII_EMAIL = "jane.doe@example.com"
_PII_NAME = "Jane Doe"
_PII_PHONE = "+15551234567"
_PII_CONSENT_TEXT = "I agree to be contacted about my inquiry."


class _StubDatabase:
    """In-memory stub database backing /admin/leads for these tests."""

    def __init__(self) -> None:
        self._leads: dict[tuple[str, str], dict[str, Any]] = {}
        self._activities: dict[tuple[str, str], dict[str, Any]] = {}
        self._users: dict[str, dict[str, Any]] = {}
        self._tenants: dict[str, dict[str, Any]] = {}
        self._activity_seq = 0

    def seed_tenant(self, tenant_id: str, *, enabled: bool = True) -> None:
        self._tenants[tenant_id] = {
            "id": tenant_id,
            "name": tenant_id,
            "slug": tenant_id,
            "enabled": enabled,
        }

    def seed_user(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        email: str = "agent@example.com",
        role: str = "CLIENT_AGENT",
        active: bool = True,
        name: str = "Agent Smith",
    ) -> None:
        self._users[user_id] = {
            "id": user_id,
            "tenant_id": tenant_id,
            "email": email,
            "role": role,
            "password_hash": "hashed",
            "name": name,
            "active": active,
            "last_login_at": None,
        }

    def seed(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        stage: str = "captured",
        status: str = "new",
        qualification_score: int | None = None,
    ) -> None:
        self._leads[(tenant_id, lead_id)] = {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "visitor_id": "visitor-1",
            "name": _PII_NAME,
            "email": _PII_EMAIL,
            "phone": _PII_PHONE,
            "status": status,
            "stage": stage,
            "qualification_score": qualification_score,
            "consent": {"granted": True, "purpose": "contact", "text": _PII_CONSENT_TEXT},
            "assigned_agent_id": None,
            "source": "widget",
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "COUNT(*)" in q and "FROM LEADS" in q:
            _, total = self._filtered_leads(query, args)
            return {"count": total}
        if "FROM LEADS" in q and "WHERE TENANT_ID" in q:
            tenant_id = args[0]
            lead_id = args[1]
            return self._leads.get((tenant_id, lead_id))
        if "FROM USERS" in q and "WHERE ID" in q:
            user_id = args[0]
            return self._users.get(user_id)
        if "FROM TENANTS" in q and "WHERE ID" in q:
            return self._tenants.get(args[0])
        return None

    def _filtered_leads(
        self, query: str, args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        q = query.upper()
        idx = 0
        tenant_id = args[idx]
        idx += 1
        rows = [row for row in self._leads.values() if row["tenant_id"] == tenant_id]

        if "STAGE = $" in q:
            rows = [r for r in rows if r["stage"] == args[idx]]
            idx += 1
        if "STATUS = $" in q:
            rows = [r for r in rows if r["status"] == args[idx]]
            idx += 1
        if "ASSIGNED_AGENT_ID = $" in q:
            rows = [r for r in rows if r["assigned_agent_id"] == args[idx]]
            idx += 1
        if "CREATED_AT >= $" in q:
            rows = [r for r in rows if r["created_at"] >= args[idx]]
            idx += 1
        if "CREATED_AT < $" in q:
            rows = [r for r in rows if r["created_at"] < args[idx]]
            idx += 1

        if "NAME ILIKE $" in q:
            pattern = str(args[idx])
            idx += 2
            needle = (
                pattern.removeprefix("%").removesuffix("%")
                .replace("\\\\", "\\").replace("\\%", "%").replace("\\_", "_")
                .lower()
            )
            rows = [
                row for row in rows
                if needle in (row["name"] or "").lower() or needle in (row["email"] or "").lower()
            ]

        if "ORDER BY " not in q:
            return rows, len(rows)

        if "(CASE STAGE" in q:
            value_for = lambda row: {"captured": 1, "qualified": 2, "contacted": 3, "converted": 4, "disqualified": 5}.get(row["stage"], 99)  # noqa: E731
        elif "(CASE STATUS" in q:
            value_for = lambda row: {"new": 1, "open": 2, "won": 3, "lost": 4}.get(row["status"], 99)  # noqa: E731
        elif "ORDER BY NAME " in q:
            value_for = lambda row: row["name"]  # noqa: E731
        elif "ORDER BY EMAIL " in q:
            value_for = lambda row: row["email"]  # noqa: E731
        elif "ORDER BY QUALIFICATION_SCORE " in q:
            value_for = lambda row: row["qualification_score"]  # noqa: E731
        elif "ORDER BY ASSIGNED_AGENT_ID " in q:
            value_for = lambda row: row["assigned_agent_id"]  # noqa: E731
        elif "ORDER BY CREATED_AT " in q:
            value_for = lambda row: row["created_at"]  # noqa: E731
        else:
            raise AssertionError(f"stub cannot honor ORDER BY: {query}")

        descending = " DESC NULLS LAST" in q
        non_null_rows = [row for row in rows if value_for(row) is not None]
        null_rows = [row for row in rows if value_for(row) is None]
        non_null_rows.sort(key=lambda row: row["lead_id"], reverse=True)
        non_null_rows.sort(key=value_for, reverse=descending)
        null_rows.sort(key=lambda row: row["lead_id"], reverse=True)
        if "(CASE STAGE" in q or "(CASE STATUS" in q:
            known_rows = [row for row in non_null_rows if value_for(row) != 99]
            unknown_rows = [row for row in non_null_rows if value_for(row) == 99]
            known_rows.sort(key=lambda row: row["lead_id"], reverse=True)
            known_rows.sort(key=value_for, reverse=descending)
            unknown_rows.sort(key=lambda row: row["lead_id"], reverse=True)
            rows = [*known_rows, *unknown_rows, *null_rows]
        else:
            rows = [*non_null_rows, *null_rows]
        total = len(rows)

        if "LIMIT $" in q:
            limit = args[idx]
            idx += 1
            offset = args[idx] if idx < len(args) else 0
            rows = rows[offset : offset + limit]

        return rows, total

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM LEAD_ACTIVITIES" in q:
            tenant_id = args[0]
            lead_id = args[1]
            rows = [
                row
                for row in self._activities.values()
                if row["tenant_id"] == tenant_id and row["lead_id"] == lead_id
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows
        if "FROM LEADS" in q and "LIMIT $" in q:
            rows, _ = self._filtered_leads(query, args)
            return rows
        if "FROM LEADS" in q:
            tenant_id = args[0]
            rows = [row for row in self._leads.values() if row["tenant_id"] == tenant_id]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("UPDATE LEADS SET ASSIGNED_AGENT_ID"):
            assigned_agent_id, tenant_id, lead_id = args
            key = (tenant_id, lead_id)
            existing = self._leads.get(key)
            if existing is None:
                return "UPDATE 0"
            existing["assigned_agent_id"] = assigned_agent_id
            existing["updated_at"] = _NOW
            return "UPDATE 1"
        if q.startswith("UPDATE LEADS"):
            stage, status, qualification_score, tenant_id, lead_id = args
            key = (tenant_id, lead_id)
            existing = self._leads.get(key)
            if existing is None:
                return "UPDATE 0"
            existing["stage"] = stage
            existing["status"] = status
            existing["qualification_score"] = qualification_score
            existing["updated_at"] = _NOW
            return "UPDATE 1"
        if q.startswith("INSERT INTO LEAD_ACTIVITIES"):
            tenant_id, activity_id, lead_id, activity_type, payload, actor = args
            self._activity_seq += 1
            self._activities[(tenant_id, activity_id)] = {
                "tenant_id": tenant_id,
                "activity_id": activity_id,
                "lead_id": lead_id,
                "type": activity_type,
                "payload": payload,
                "actor": actor,
                "created_at": _NOW.replace(microsecond=self._activity_seq),
            }
            return "INSERT 0 1"
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
    return app


def _token(role: Role, tenant_id: str | None = _TENANT_ID, subject: str = "user-1") -> str:
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
# GET /admin/leads/{lead_id}
# ---------------------------------------------------------------------------


async def test_get_lead_returns_200_with_stage_status_score(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-1"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new", qualification_score=70)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            f"/admin/leads/{lead_id}", cookies={"access_token": token}
        )

    assert response.status_code == 200
    data = response.json()
    assert data["stage"] == "captured"
    assert data["status"] == "new"
    assert data["qualification_score"] == 70
    assert "tenant_id" not in data


async def test_get_unknown_lead_returns_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/leads/does-not-exist", cookies={"access_token": token}
        )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /admin/leads/{lead_id}
# ---------------------------------------------------------------------------


async def test_patch_valid_transition_returns_200_with_recomputed_fields(
    app: Any, db: _StubDatabase
) -> None:
    lead_id = "lead-2"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new", qualification_score=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["stage"] == "qualified"
    assert data["status"] == "open"
    assert isinstance(data["qualification_score"], int)
    assert "tenant_id" not in data


async def test_patch_score_increases_moving_forward(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-3"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new", qualification_score=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        first = await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )
        second = await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "contacted"},
            cookies={"access_token": token},
        )

    assert second.json()["qualification_score"] > first.json()["qualification_score"]


async def test_patch_illegal_transition_returns_422_and_nothing_persisted(
    app: Any, db: _StubDatabase
) -> None:
    lead_id = "lead-4"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new", qualification_score=42)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "converted"},  # skip -- illegal
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    data = response.json()
    assert data["error_code"] == "INVALID_STAGE_TRANSITION"
    # Nothing persisted
    stored = db._leads[(_TENANT_ID, lead_id)]
    assert stored["stage"] == "captured"
    assert stored["qualification_score"] == 42


async def test_patch_terminal_stage_transition_returns_422(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-5"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="converted", status="won", qualification_score=90)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_STAGE_TRANSITION"


async def test_patch_unknown_lead_returns_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.patch(
            "/admin/leads/does-not-exist",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_client_agent_can_patch(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-6"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200


async def test_client_admin_can_get(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-7"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            f"/admin/leads/{lead_id}", cookies={"access_token": token}
        )

    assert response.status_code == 200


async def test_visitor_forbidden(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-8"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get(
            f"/admin/leads/{lead_id}", cookies={"access_token": token}
        )

    assert response.status_code == 403


async def test_no_auth_returns_401(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-9"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/admin/leads/{lead_id}")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_cross_tenant_get_returns_404(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-10"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, tenant_id=_OTHER_TENANT_ID)
        response = await client.get(
            f"/admin/leads/{lead_id}", cookies={"access_token": token}
        )

    assert response.status_code == 404


async def test_cross_tenant_patch_returns_404(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-11"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, tenant_id=_OTHER_TENANT_ID)
        response = await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404
    # Original tenant's lead is unchanged
    stored = db._leads[(_TENANT_ID, lead_id)]
    assert stored["stage"] == "captured"


# ---------------------------------------------------------------------------
# PII discipline
# ---------------------------------------------------------------------------


async def test_pii_not_logged_on_transition(app: Any, db: _StubDatabase, caplog: Any) -> None:
    lead_id = "lead-12"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new")

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.patch(
                f"/admin/leads/{lead_id}",
                json={"stage": "qualified"},
                cookies={"access_token": token},
            )

    log_text = caplog.text
    assert _PII_EMAIL not in log_text
    assert _PII_NAME not in log_text
    assert _PII_PHONE not in log_text
    assert _PII_CONSENT_TEXT not in log_text


# ---------------------------------------------------------------------------
# stage_change activity on PATCH (S7.3 decision 4)
# ---------------------------------------------------------------------------


async def test_patch_stage_transition_appends_stage_change_activity(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-13"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.patch(
            f"/admin/leads/{lead_id}",
            json={"stage": "qualified"},
            cookies={"access_token": token},
        )
        activities_response = await client.get(
            f"/admin/leads/{lead_id}/activities", cookies={"access_token": token}
        )

    assert activities_response.status_code == 200
    activities = activities_response.json()
    assert len(activities) == 1
    assert activities[0]["type"] == "stage_change"
    assert activities[0]["payload"] == {"from_stage": "captured", "to_stage": "qualified"}


# ---------------------------------------------------------------------------
# POST /admin/leads/{lead_id}/notes
# ---------------------------------------------------------------------------


async def test_post_note_valid_returns_201_activity(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-14"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, subject="admin-user-1")
        response = await client.post(
            f"/admin/leads/{lead_id}/notes",
            json={"text": "Called, left voicemail."},
            cookies={"access_token": token},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["type"] == "note"
    assert data["payload"] == {"text": "Called, left voicemail."}
    assert data["actor"] == "admin-user-1"


async def test_post_note_blank_returns_422(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-15"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            f"/admin/leads/{lead_id}/notes",
            json={"text": "   "},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_post_note_too_long_returns_422(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-15b"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            f"/admin/leads/{lead_id}/notes",
            json={"text": "x" * 4001},
            cookies={"access_token": token},
        )

    assert response.status_code == 422


async def test_post_note_unknown_lead_returns_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/does-not-exist/notes",
            json={"text": "hello"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404


async def test_post_note_pii_not_logged(app: Any, db: _StubDatabase, caplog: Any) -> None:
    lead_id = "lead-15c"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    secret_note = "Client SSN is 123-45-6789, very sensitive."

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            await client.post(
                f"/admin/leads/{lead_id}/notes",
                json={"text": secret_note},
                cookies={"access_token": token},
            )

    assert secret_note not in caplog.text


# ---------------------------------------------------------------------------
# POST /admin/leads/{lead_id}/assignment
# ---------------------------------------------------------------------------


async def test_post_assignment_valid_agent_returns_200(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-16"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, role="CLIENT_AGENT", active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            f"/admin/leads/{lead_id}/assignment",
            json={"agent_id": "agent-1"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["assigned_agent_id"] == "agent-1"

    stored = db._leads[(_TENANT_ID, lead_id)]
    assert stored["assigned_agent_id"] == "agent-1"


async def test_post_assignment_appends_activity(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-16b"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, role="CLIENT_AGENT", active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN, subject="admin-user-2")
        await client.post(
            f"/admin/leads/{lead_id}/assignment",
            json={"agent_id": "agent-1"},
            cookies={"access_token": token},
        )
        activities_response = await client.get(
            f"/admin/leads/{lead_id}/activities", cookies={"access_token": token}
        )

    activities = activities_response.json()
    assert len(activities) == 1
    assert activities[0]["type"] == "assignment"
    assert activities[0]["payload"] == {"agent_id": "agent-1", "previous_agent_id": None}
    assert activities[0]["actor"] == "admin-user-2"


@pytest.mark.parametrize(
    "seed_kwargs",
    [
        pytest.param(None, id="not-found"),
        pytest.param({"tenant_id": _OTHER_TENANT_ID}, id="other-tenant"),
        pytest.param({"tenant_id": _TENANT_ID, "role": "CLIENT_ADMIN"}, id="wrong-role"),
        pytest.param({"tenant_id": _TENANT_ID, "role": "PLATFORM_ADMIN"}, id="platform-admin"),
        pytest.param({"tenant_id": _TENANT_ID, "active": False}, id="inactive"),
    ],
)
async def test_post_assignment_invalid_assignee_returns_422(
    app: Any, db: _StubDatabase, seed_kwargs: dict[str, Any] | None
) -> None:
    lead_id = "lead-17"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    if seed_kwargs is not None:
        db.seed_user(user_id="bad-agent", **seed_kwargs)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            f"/admin/leads/{lead_id}/assignment",
            json={"agent_id": "bad-agent"},
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_ASSIGNEE"
    stored = db._leads[(_TENANT_ID, lead_id)]
    assert stored["assigned_agent_id"] is None
    assert db._activities == {}


async def test_post_assignment_unknown_lead_returns_404(app: Any, db: _StubDatabase) -> None:
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, role="CLIENT_AGENT", active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.post(
            "/admin/leads/does-not-exist/assignment",
            json={"agent_id": "agent-1"},
            cookies={"access_token": token},
        )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /admin/leads/{lead_id}/activities
# ---------------------------------------------------------------------------


async def test_get_activities_returns_ordered_timeline(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-18"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, role="CLIENT_AGENT", active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        await client.post(
            f"/admin/leads/{lead_id}/notes",
            json={"text": "first note"},
            cookies={"access_token": token},
        )
        await client.post(
            f"/admin/leads/{lead_id}/assignment",
            json={"agent_id": "agent-1"},
            cookies={"access_token": token},
        )
        response = await client.get(
            f"/admin/leads/{lead_id}/activities", cookies={"access_token": token}
        )

    assert response.status_code == 200
    activities = response.json()
    assert len(activities) == 2
    # newest first: assignment (added second) before note (added first)
    assert activities[0]["type"] == "assignment"
    assert activities[1]["type"] == "note"


async def test_get_activities_unknown_lead_returns_404(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/leads/does-not-exist/activities", cookies={"access_token": token}
        )

    assert response.status_code == 404


async def test_get_activities_cross_tenant_returns_404(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-19"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, tenant_id=_OTHER_TENANT_ID)
        response = await client.get(
            f"/admin/leads/{lead_id}/activities", cookies={"access_token": token}
        )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# RBAC on the new endpoints
# ---------------------------------------------------------------------------


async def test_notes_visitor_forbidden(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-20"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.post(
            f"/admin/leads/{lead_id}/notes",
            json={"text": "hello"},
            cookies={"access_token": token},
        )

    assert response.status_code == 403


async def test_notes_no_auth_returns_401(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-21"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/admin/leads/{lead_id}/notes",
            json={"text": "hello"},
        )

    assert response.status_code == 401


async def test_assignment_client_agent_allowed(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-22"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, role="CLIENT_AGENT", active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.post(
            f"/admin/leads/{lead_id}/assignment",
            json={"agent_id": "agent-1"},
            cookies={"access_token": token},
        )

    assert response.status_code == 200


async def test_activities_visitor_forbidden(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-23"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get(
            f"/admin/leads/{lead_id}/activities", cookies={"access_token": token}
        )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /admin/leads/export
# ---------------------------------------------------------------------------


async def test_export_returns_200_text_csv(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-e1", stage="captured", status="new", qualification_score=10)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads/export", cookies={"access_token": token})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]


async def test_export_contains_tenant_leads_only(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-e2", stage="captured", status="new")
    db.seed(tenant_id=_OTHER_TENANT_ID, lead_id="lead-e3", stage="captured", status="new")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads/export", cookies={"access_token": token})

    body = response.text
    assert "lead-e2" in body
    assert "lead-e3" not in body


async def test_export_excludes_consent_text(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-e4", stage="captured", status="new")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads/export", cookies={"access_token": token})

    assert _PII_CONSENT_TEXT not in response.text


async def test_export_columns_present(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-e5", stage="captured", status="new", qualification_score=55)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads/export", cookies={"access_token": token})

    header_line = response.text.splitlines()[0]
    for col in (
        "lead_id", "name", "email", "phone", "status", "stage",
        "qualification_score", "assigned_agent_id", "source", "created_at",
    ):
        assert col in header_line


async def test_export_client_agent_allowed(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-e6")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/leads/export", cookies={"access_token": token})

    assert response.status_code == 200


async def test_export_visitor_forbidden(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/leads/export", cookies={"access_token": token})

    assert response.status_code == 403


async def test_export_no_auth_returns_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/leads/export")

    assert response.status_code == 401


async def test_export_not_matched_as_lead_id_path(app: Any, db: _StubDatabase) -> None:
    """'/export' must route to the export endpoint, not GET /{lead_id}=export -> 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads/export", cookies={"access_token": token})

    # A 404 here would mean the route ordering treated "export" as a lead_id.
    assert response.status_code != 404


# ---------------------------------------------------------------------------
# GET /admin/leads (S12.4 -- paginated, filterable list)
# ---------------------------------------------------------------------------


async def test_list_leads_happy_path_client_admin(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-l1", stage="captured", status="new", qualification_score=10)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads", cookies={"access_token": token})

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"items", "total", "limit", "offset"}
    assert data["total"] == 1
    assert data["limit"] == 50
    assert data["offset"] == 0
    item = data["items"][0]
    assert item["lead_id"] == "lead-l1"
    assert item["email"] == _PII_EMAIL
    assert "created_at" in item
    assert "tenant_id" not in item
    assert "consent" not in item


async def test_list_leads_client_agent_allowed(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-l2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT)
        response = await client.get("/admin/leads", cookies={"access_token": token})

    assert response.status_code == 200


async def test_list_leads_visitor_forbidden(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.VISITOR)
        response = await client.get("/admin/leads", cookies={"access_token": token})

    assert response.status_code == 403


async def test_list_leads_no_auth_returns_401(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/leads")

    assert response.status_code == 401


async def test_list_leads_platform_admin_forbidden(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.PLATFORM_ADMIN, tenant_id=None)
        response = await client.get("/admin/leads", cookies={"access_token": token})

    assert response.status_code == 403
    assert response.json()["error_code"] == "ROLE_NOT_PERMITTED"


async def test_list_leads_invalid_stage_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads?stage=bogus", cookies={"access_token": token})

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LEAD_FILTER"


async def test_list_leads_invalid_status_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads?status=bogus", cookies={"access_token": token})

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LEAD_FILTER"


async def test_list_leads_valid_stage_status_returns_200(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-l3", stage="qualified", status="open")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/leads?stage=qualified&status=open", cookies={"access_token": token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["lead_id"] == "lead-l3"


async def test_list_leads_assigned_agent_id_no_match_returns_empty_page(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-l4")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/leads?assigned_agent_id=no-such-agent", cookies={"access_token": token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


async def test_list_leads_from_gte_to_returns_422(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get(
            "/admin/leads?from=2026-06-01T00:00:00Z&to=2026-01-01T00:00:00Z",
            cookies={"access_token": token},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LIST_WINDOW"


async def test_list_leads_limit_clamped_to_200(app: Any, db: _StubDatabase) -> None:
    for i in range(3):
        db.seed(tenant_id=_TENANT_ID, lead_id=f"lead-clamp-{i}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads?limit=9999", cookies={"access_token": token})

    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 200
    assert len(data["items"]) <= 200


async def test_list_leads_limit_zero_clamped_to_1(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads?limit=0", cookies={"access_token": token})

    assert response.status_code == 200
    assert response.json()["limit"] == 1


async def test_list_leads_negative_limit_clamped_to_1(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads?limit=-5", cookies={"access_token": token})

    assert response.status_code == 200
    assert response.json()["limit"] == 1


async def test_list_leads_negative_offset_clamped_to_0(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_ADMIN)
        response = await client.get("/admin/leads?offset=-5", cookies={"access_token": token})

    assert response.status_code == 200
    assert response.json()["offset"] == 0


async def test_list_leads_cross_tenant_isolation(app: Any, db: _StubDatabase) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="lead-mine")
    db.seed(tenant_id=_OTHER_TENANT_ID, lead_id="lead-other")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = _token(Role.CLIENT_AGENT, tenant_id=_TENANT_ID)
        response = await client.get("/admin/leads", cookies={"access_token": token})

    assert response.status_code == 200
    ids = [item["lead_id"] for item in response.json()["items"]]
    assert ids == ["lead-mine"]


# ---------------------------------------------------------------------------
# SR-25: list sort + combined name/email search contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "created_at; DROP TABLE leads--",
        "created_at) --",
        "name, tenant_id",
        "(SELECT tenant_id)",
        "created_at DESC; SELECT * FROM users",
        "tenant_id",
        "1",
        "name/**/",
        "",
        "NAME",
    ],
)
async def test_list_leads_sort_rejects_unknown_or_hostile_keys(
    app: Any, db: _StubDatabase, payload: str,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/leads",
            params={"sort": payload},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LEAD_SORT"


async def test_list_leads_sort_rejects_unknown_direction(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/leads",
            params={"sort": "score", "dir": "sideways"},
            cookies={"access_token": _token(Role.CLIENT_ADMIN)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LEAD_SORT_DIRECTION"


@pytest.mark.parametrize("sort", ["name", "email", "stage", "status", "score", "assigned", "created"])
async def test_list_leads_all_sort_keys_are_accepted(app: Any, db: _StubDatabase, sort: str) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id=f"lead-sort-{sort}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/leads",
            params={"sort": sort, "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200


async def test_list_leads_q_too_short_is_rejected(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/leads", params={"q": "a"}, cookies={"access_token": _token(Role.CLIENT_ADMIN)}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LEAD_SEARCH"


async def test_list_leads_q_too_long_is_rejected(app: Any, db: _StubDatabase) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/leads", params={"q": "a" * 201}, cookies={"access_token": _token(Role.CLIENT_ADMIN)}
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LEAD_SEARCH"


async def test_list_leads_q_matches_name_or_email_without_cross_tenant_leak(
    app: Any, db: _StubDatabase,
) -> None:
    db.seed(tenant_id=_TENANT_ID, lead_id="needle-name")
    db._leads[(_TENANT_ID, "needle-name")]["name"] = "Alice Needle"
    db.seed(tenant_id=_OTHER_TENANT_ID, lead_id="needle-other")
    db._leads[(_OTHER_TENANT_ID, "needle-other")]["email"] = "alice.needle@example.com"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/leads",
            params={"q": "NEEDLE", "sort": "name", "dir": "asc"},
            cookies={"access_token": _token(Role.CLIENT_AGENT)},
        )

    assert response.status_code == 200
    assert [item["lead_id"] for item in response.json()["items"]] == ["needle-name"]


async def test_tenant_scoped_list_route_accepts_sort_and_search(app: Any, db: _StubDatabase) -> None:
    db.seed_tenant(_TENANT_ID)
    db.seed(tenant_id=_TENANT_ID, lead_id="tenant-route")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/leads",
            params={"sort": "score", "dir": "desc", "q": "jane"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 200
    assert [item["lead_id"] for item in response.json()["items"]] == ["tenant-route"]


async def test_tenant_scoped_list_route_rejects_unknown_sort(app: Any, db: _StubDatabase) -> None:
    db.seed_tenant(_TENANT_ID)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/admin/tenants/{_TENANT_ID}/leads",
            params={"sort": "tenant_id"},
            cookies={"access_token": _token(Role.PLATFORM_ADMIN, tenant_id=None)},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_LEAD_SORT"


# ---------------------------------------------------------------------------
# SR-21: feed emits (D2/D3) -- lead_assigned, lead_stage_transitioned
# ---------------------------------------------------------------------------


async def test_patch_stage_transition_emits_lead_stage_transitioned(
    app: Any, db: _StubDatabase,
) -> None:
    lead_id = "lead-emit-stage"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new")

    with patch("api.leads.admin_routes.emit_event_safe") as mock_emit:
        mock_emit.return_value = "event-1"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.patch(
                f"/admin/leads/{lead_id}", json={"stage": "qualified"},
                cookies={"access_token": token},
            )

    assert response.status_code == 200
    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "lead_stage_transitioned"
    assert kwargs["category"] == "leads"
    assert kwargs["target_id"] == lead_id
    assert kwargs["payload"] == {"lead_id": lead_id, "from_stage": "captured", "to_stage": "qualified"}


async def test_patch_stage_transition_still_200_when_feed_emit_raises(
    app: Any, db: _StubDatabase,
) -> None:
    """MANDATORY (D2): a feed-insert failure must not fail the transition --
    it commits and returns 200 with only the feed item missing."""
    lead_id = "lead-emit-stage-2"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id, stage="captured", status="new")

    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.patch(
                f"/admin/leads/{lead_id}", json={"stage": "qualified"},
                cookies={"access_token": token},
            )

    assert response.status_code == 200
    assert response.json()["stage"] == "qualified"
    assert db._leads[(_TENANT_ID, lead_id)]["stage"] == "qualified"


async def test_post_assignment_emits_lead_assigned(app: Any, db: _StubDatabase) -> None:
    lead_id = "lead-emit-assign"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, role="CLIENT_AGENT", active=True)

    with patch("api.leads.admin_routes.emit_event_safe") as mock_emit:
        mock_emit.return_value = "event-1"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                f"/admin/leads/{lead_id}/assignment", json={"agent_id": "agent-1"},
                cookies={"access_token": token},
            )

    assert response.status_code == 200
    mock_emit.assert_awaited_once()
    _, kwargs = mock_emit.call_args
    assert kwargs["kind"] == "lead_assigned"
    assert kwargs["category"] == "leads"
    assert kwargs["target_id"] == lead_id
    assert kwargs["payload"] == {"lead_id": lead_id, "agent_id": "agent-1"}


async def test_post_assignment_still_200_when_feed_emit_raises(app: Any, db: _StubDatabase) -> None:
    """MANDATORY (D2): a feed-insert failure must not fail the assignment."""
    lead_id = "lead-emit-assign-2"
    db.seed(tenant_id=_TENANT_ID, lead_id=lead_id)
    db.seed_user(user_id="agent-1", tenant_id=_TENANT_ID, role="CLIENT_AGENT", active=True)

    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("feed insert exploded")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = _token(Role.CLIENT_ADMIN)
            response = await client.post(
                f"/admin/leads/{lead_id}/assignment", json={"agent_id": "agent-1"},
                cookies={"access_token": token},
            )

    assert response.status_code == 200
    assert db._leads[(_TENANT_ID, lead_id)]["assigned_agent_id"] == "agent-1"
