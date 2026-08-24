"""Unit tests for api.training.routes.

Mocks the individual repository/service functions the routes call (same
style as test_voice_routes.py), rather than a full stub DB -- each route is a
thin composition over already-tested functions, so these tests verify the
composition + RBAC + response shape, not the underlying SQL.

Covers:
- POST /admin/training/chat: 200 shape from preview_answer; never touches
  conversation_store (asserted by NOT mocking append_message/
  create_conversation and confirming no AttributeError-triggering call
  would occur -- preview_answer itself is mocked here, so the real
  no-persistence guarantee is covered by test_orchestrator_preview.py;
  this file only proves the route wires the call through cleanly).
- GET /admin/training/gaps: excludes already-taught questions (mandatory).
- POST /admin/training/answer: creates doc + run + training_answers row +
  audit event on a fresh question; idempotent re-teach (same Q&A text) skips
  storage/enqueue, matching upload's own idempotent-reupload contract.
- RBAC: CLIENT_ADMIN 200, CLIENT_AGENT 403, no cookie 401 (one representative
  route each, matching test_ingestion_routes.py's convention).
"""
from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.conversation_store.repository import CoverageGap
from api.orchestrator.service import PreviewResult
from api.training.repository import TrainingAnswer

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

_TENANT_ID = "tenant-training"


class _StubDatabase:
    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
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


def _build_app() -> Any:
    _reset_settings()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.app import create_app

        app = create_app()
        app.state.db = _StubDatabase()
        app.state.redis = _StubRedis()
        app.state.cache = InMemoryCache()
        app.state.rate_limiter = None
        return app


def _mint_cookie(*, role: Role = Role.CLIENT_ADMIN, tenant_id: str | None = _TENANT_ID) -> str:
    from api.auth.tokens import create_access_token

    claims = AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret="x" * 48, ttl_seconds=300)
    return token


# ==============================================================================
# POST /admin/training/chat
# ==============================================================================


async def test_chat_returns_preview_result_shape() -> None:
    app = _build_app()
    token = _mint_cookie()

    result = PreviewResult(
        reply="We're open Monday through Friday.",
        decision="answer",
        confidence=0.82,
        sources=[],
    )
    with patch("api.training.routes.preview_answer", AsyncMock(return_value=result)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/training/chat",
                cookies={"access_token": token},
                json={"message": "what are your hours?"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "reply": "We're open Monday through Friday.",
        "decision": "answer",
        "confidence": 0.82,
        "sources": [],
    }


async def test_chat_blank_message_422() -> None:
    app = _build_app()
    token = _mint_cookie()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/training/chat", cookies={"access_token": token}, json={"message": "   "},
        )
    assert resp.status_code == 422


async def test_chat_client_agent_403() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/training/chat", cookies={"access_token": token}, json={"message": "hi"},
        )
    assert resp.status_code == 403


async def test_chat_no_cookie_401() -> None:
    app = _build_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/admin/training/chat", json={"message": "hi"})
    assert resp.status_code == 401


# ==============================================================================
# GET /admin/training/gaps
# ==============================================================================


async def test_gaps_excludes_already_taught_questions() -> None:
    """Mandatory case: a question already covered by a training_answers row
    must not keep showing up as a gap."""
    app = _build_app()
    token = _mint_cookie()

    gaps = [
        CoverageGap(
            message_id="m1",
            decision="escalate",
            confidence=0.1,
            created_at=__import__("datetime").datetime(2026, 1, 1),
            question="How much does an inspection cost?",
            question_message_id="q1",
        ),
        CoverageGap(
            message_id="m2",
            decision="clarify",
            confidence=0.4,
            created_at=__import__("datetime").datetime(2026, 1, 2),
            question="  Already   Taught Question  ",
            question_message_id="q2",
        ),
    ]
    with (
        patch("api.training.routes.list_low_confidence_messages", AsyncMock(return_value=gaps)),
        patch(
            "api.training.routes.list_taught_question_keys",
            AsyncMock(return_value={"already taught question"}),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/admin/training/gaps", cookies={"access_token": token})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["gaps"]) == 1
    assert body["gaps"][0]["question"] == "How much does an inspection cost?"


async def test_gaps_client_agent_403() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/training/gaps", cookies={"access_token": token})
    assert resp.status_code == 403


# ==============================================================================
# POST /admin/training/answer
# ==============================================================================


async def test_answer_teaches_a_fresh_question() -> None:
    """Fresh Q&A -> stores raw bytes, creates doc + run, enqueues ingestion,
    records a training_answers row + audit event."""
    app = _build_app()
    token = _mint_cookie()

    stub_storage = MagicMock()
    fake_run = MagicMock(run_id="run-1")
    fake_training_answer = TrainingAnswer(
        id="ta-1",
        question="How much does an inspection cost?",
        answer="Inspections are free.",
        source_message_id="q1",
        doc_id="doc-1",
        created_by="admin-1",
        created_at=__import__("datetime").datetime(2026, 1, 1),
        dismissed=False,
    )

    with (
        patch("api.training.routes.ingestion_repo.find_doc_by_hash", AsyncMock(return_value=None)),
        patch("api.training.routes.get_storage", return_value=stub_storage),
        patch("api.training.routes.ingestion_repo.create_doc", AsyncMock()) as mock_create_doc,
        patch("api.training.routes.ingestion_repo.create_run", AsyncMock(return_value=fake_run)),
        patch("api.training.routes.ingest_document") as mock_task,
        patch(
            "api.training.routes.create_training_answer",
            AsyncMock(return_value=fake_training_answer),
        ) as mock_create_answer,
        patch("api.training.routes.record_audit", AsyncMock()) as mock_audit,
    ):
        mock_task.delay = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/training/answer",
                cookies={"access_token": token},
                json={
                    "question": "How much does an inspection cost?",
                    "answer": "Inspections are free.",
                    "source_message_id": "q1",
                },
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["training_answer_id"] == "ta-1"
    assert re.fullmatch(r"[0-9a-f]{32}", body["doc_id"])
    assert stub_storage.put.called
    assert mock_create_doc.await_args.kwargs["source"] == "training"
    assert mock_create_doc.await_args.kwargs["doc_id"] == body["doc_id"]
    assert mock_task.delay.called
    assert mock_create_answer.await_args.kwargs["question"] == "How much does an inspection cost?"
    assert mock_audit.await_args.kwargs["action"] == "training_answer_created"


async def test_answer_idempotent_reteach_skips_storage_and_enqueue() -> None:
    """Same Q&A text (same content hash) -> reuses the existing doc, no new
    storage write, no second Celery enqueue -- mirrors upload's own
    idempotent-reupload contract exactly."""
    app = _build_app()
    token = _mint_cookie()

    existing_doc = MagicMock(doc_id="doc-existing")
    fake_training_answer = TrainingAnswer(
        id="ta-2",
        question="q",
        answer="a",
        source_message_id=None,
        doc_id="doc-existing",
        created_by="admin-1",
        created_at=__import__("datetime").datetime(2026, 1, 1),
        dismissed=False,
    )

    with (
        patch(
            "api.training.routes.ingestion_repo.find_doc_by_hash", AsyncMock(return_value=existing_doc),
        ),
        patch("api.training.routes.get_storage") as mock_get_storage,
        patch("api.training.routes.ingestion_repo.create_doc", AsyncMock()) as mock_create_doc,
        patch("api.training.routes.ingestion_repo.create_run", AsyncMock()) as mock_create_run,
        patch("api.training.routes.ingest_document") as mock_task,
        patch(
            "api.training.routes.create_training_answer",
            AsyncMock(return_value=fake_training_answer),
        ),
        patch("api.training.routes.record_audit", AsyncMock()),
    ):
        mock_task.delay = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/training/answer",
                cookies={"access_token": token},
                json={"question": "q", "answer": "a"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == "doc-existing"
    assert body["run_id"] is None
    mock_get_storage.assert_not_called()
    mock_create_doc.assert_not_called()
    mock_create_run.assert_not_called()
    mock_task.delay.assert_not_called()


async def test_answer_blank_question_422() -> None:
    app = _build_app()
    token = _mint_cookie()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/training/answer",
            cookies={"access_token": token},
            json={"question": "   ", "answer": "a"},
        )
    assert resp.status_code == 422


async def test_answer_client_agent_403() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/training/answer",
            cookies={"access_token": token},
            json={"question": "q", "answer": "a"},
        )
    assert resp.status_code == 403


# ==============================================================================
# POST /admin/training/dismiss
# ==============================================================================


async def test_dismiss_records_a_dismissal_no_ingestion_touched() -> None:
    """Dismissing a junk gap must never touch storage/create_doc/enqueue --
    only a training_answers row + audit event."""
    app = _build_app()
    token = _mint_cookie()

    fake_training_answer = TrainingAnswer(
        id="ta-3",
        question="I won't.",
        answer=None,
        source_message_id="q1",
        doc_id=None,
        created_by="admin-1",
        created_at=__import__("datetime").datetime(2026, 1, 1),
        dismissed=True,
    )

    with (
        patch("api.training.routes.get_storage") as mock_get_storage,
        patch("api.training.routes.ingestion_repo.create_doc", AsyncMock()) as mock_create_doc,
        patch("api.training.routes.ingest_document") as mock_task,
        patch(
            "api.training.routes.create_training_answer",
            AsyncMock(return_value=fake_training_answer),
        ) as mock_create_answer,
        patch("api.training.routes.record_audit", AsyncMock()) as mock_audit,
    ):
        mock_task.delay = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/training/dismiss",
                cookies={"access_token": token},
                json={"question": "I won't.", "source_message_id": "q1"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"training_answer_id": "ta-3"}
    mock_get_storage.assert_not_called()
    mock_create_doc.assert_not_called()
    mock_task.delay.assert_not_called()
    assert mock_create_answer.await_args.kwargs["answer"] is None
    assert mock_create_answer.await_args.kwargs["doc_id"] is None
    assert mock_create_answer.await_args.kwargs["dismissed"] is True
    assert mock_audit.await_args.kwargs["action"] == "training_gap_dismissed"


async def test_dismiss_blank_question_422() -> None:
    app = _build_app()
    token = _mint_cookie()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/training/dismiss", cookies={"access_token": token}, json={"question": "   "},
        )
    assert resp.status_code == 422


async def test_dismiss_client_agent_403() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/admin/training/dismiss", cookies={"access_token": token}, json={"question": "q"},
        )
    assert resp.status_code == 403


async def test_dismiss_no_cookie_401() -> None:
    app = _build_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/admin/training/dismiss", json={"question": "q"})
    assert resp.status_code == 401
