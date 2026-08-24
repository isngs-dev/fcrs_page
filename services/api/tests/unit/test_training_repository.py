"""Unit tests for the training repository.

Covers:
- normalize_question: lowercase + whitespace-collapse.
- create_training_answer: inserts with the caller's tenant_id + subject;
  normalizes the question before storing.
- list_taught_question_keys: filters by tenant_id.
- Global caller (PLATFORM_ADMIN) -> ValidationError on every method.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.training.repository import (
    create_training_answer,
    list_taught_question_keys,
    normalize_question,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _RecordingDatabase:
    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.last_sql: str = ""
        self.last_params: tuple[Any, ...] = ()
        self.last_execute_params: tuple[Any, ...] = ()
        self._rows = rows or []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.last_sql = query
        self.last_params = args
        return self._rows[0] if self._rows else None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.last_sql = query
        self.last_params = args
        return self._rows

    async def execute(self, query: str, *args: Any) -> str:
        self.last_sql = query
        self.last_params = args
        self.last_execute_params = args
        return "INSERT 1"


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN, subject: str = "admin-1") -> AuthClaims:
    return AuthClaims(subject=subject, role=role, tenant_id=tenant_id)


# -- normalize_question ---------------------------------------------------------


def test_normalize_question_lowercases_and_collapses_whitespace() -> None:
    assert normalize_question("  How Much   Does\tan Inspection COST?  ") == "how much does an inspection cost?"


def test_normalize_question_identical_for_reworded_whitespace() -> None:
    assert normalize_question("A  B") == normalize_question("a b")


# -- create_training_answer ------------------------------------------------------


async def test_create_training_answer_inserts_with_callers_tenant_and_subject() -> None:
    row = {
        "id": "ta-1",
        "question": "How much?",
        "answer": "Free.",
        "source_message_id": None,
        "doc_id": "doc-1",
        "created_by": "admin-1",
        "created_at": _NOW,
        "dismissed": False,
    }
    db = _RecordingDatabase(rows=[row])
    claims = _claims("tenant-a", subject="admin-1")

    result = await create_training_answer(db, claims, question="How much?", answer="Free.", doc_id="doc-1")

    assert result.id == "ta-1"
    assert result.dismissed is False
    assert "tenant_id" in db.last_sql
    assert "training_answers" in db.last_sql


async def test_create_training_answer_normalizes_question_before_insert() -> None:
    db = _RecordingDatabase(rows=[{
        "id": "ta-1", "question": "Q", "answer": "A", "source_message_id": None,
        "doc_id": None, "created_by": "admin-1", "created_at": _NOW, "dismissed": False,
    }])
    claims = _claims("tenant-a")

    await create_training_answer(db, claims, question="  How Much?  ", answer="A", doc_id=None)

    # execute() params order: tenant_id, id, question, question_normalized, answer, source_message_id, doc_id, created_by, dismissed
    assert db.last_execute_params[3] == "how much?"


async def test_create_training_answer_rejects_global_caller() -> None:
    db = _RecordingDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await create_training_answer(db, claims, question="q", answer="a", doc_id=None)


async def test_create_training_answer_dismissal_stores_no_answer_or_doc() -> None:
    """A dismissal (Section 4's "not a real gap" case): answer/doc_id are
    NULL, dismissed=True -- the row still counts as "handled" for the
    coverage-gaps filter, but no knowledge_docs row is ever touched."""
    db = _RecordingDatabase(rows=[{
        "id": "ta-2", "question": "I won't.", "answer": None, "source_message_id": "q1",
        "doc_id": None, "created_by": "admin-1", "created_at": _NOW, "dismissed": True,
    }])
    claims = _claims("tenant-a")

    result = await create_training_answer(
        db, claims, question="I won't.", answer=None, doc_id=None,
        source_message_id="q1", dismissed=True,
    )

    assert result.dismissed is True
    assert result.answer is None
    assert result.doc_id is None
    # execute() params order: ..., answer($5), source_message_id($6), doc_id($7), created_by($8), dismissed($9)
    assert db.last_execute_params[4] is None
    assert db.last_execute_params[6] is None
    assert db.last_execute_params[8] is True


# -- list_taught_question_keys ---------------------------------------------------


async def test_list_taught_question_keys_returns_normalized_set() -> None:
    db = _RecordingDatabase(rows=[
        {"question_normalized": "how much?"},
        {"question_normalized": "when open?"},
    ])
    claims = _claims("tenant-a")

    keys = await list_taught_question_keys(db, claims)

    assert keys == {"how much?", "when open?"}
    assert db.last_params[0] == "tenant-a"


async def test_list_taught_question_keys_rejects_global_caller() -> None:
    db = _RecordingDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError):
        await list_taught_question_keys(db, claims)
