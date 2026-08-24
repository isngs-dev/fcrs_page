"""Training repository — tenant-scoped async SQL for training_answers.

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …),
  building them with ``f"${len(params)}"`` — never a hardcoded index.
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

Data model (migrations 0056, 0057):
- ``training_answers(tenant_id PK, id PK, question, question_normalized,
  answer NULL, source_message_id, doc_id, created_by, created_at,
  dismissed)`` — composite FK ``(tenant_id, doc_id) -> knowledge_docs
  (tenant_id, doc_id)``. A row is either a taught answer (``dismissed=false``,
  ``answer``/``doc_id`` set) or a dismissal (``dismissed=true``, ``answer``/
  ``doc_id`` NULL) -- a DB CHECK constraint enforces one of the two is
  always true. Both kinds count as "handled" for the coverage-gaps filter,
  which only cares whether ANY row exists for a given normalized question.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN)."""
    if claims.tenant_id is None:
        raise ValidationError("Training is tenant-scoped.")


def normalize_question(question: str) -> str:
    """Lowercase + collapse whitespace -- the exact-match dedup key used to
    keep an already-taught question off the coverage-gaps list. A deliberate
    ceiling (not fuzzy/embedding-based matching): two differently-worded
    questions with the same underlying answer are NOT deduped against each
    other. Upgrade path if that becomes real noise: a pgvector similarity
    lookup instead of this string compare.
    """
    return " ".join(question.lower().split())


@dataclass(frozen=True)
class TrainingAnswer:
    id: str
    question: str
    answer: str | None
    source_message_id: str | None
    doc_id: str | None
    created_by: str
    created_at: datetime
    dismissed: bool


async def create_training_answer(
    db: Database,
    claims: AuthClaims,
    *,
    question: str,
    answer: str | None,
    doc_id: str | None,
    source_message_id: str | None = None,
    dismissed: bool = False,
) -> TrainingAnswer:
    """Insert a new ``training_answers`` row -- a taught answer
    (``dismissed=False``, the default; ``answer``/``doc_id`` required) or a
    dismissal (``dismissed=True``; ``answer``/``doc_id`` typically ``None``).
    Returns the resulting row.
    """
    _reject_global(claims)

    new_id = uuid4().hex
    question_normalized = normalize_question(question)

    await db.execute(
        "INSERT INTO training_answers "
        "(tenant_id, id, question, question_normalized, answer, "
        " source_message_id, doc_id, created_by, dismissed) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        claims.tenant_id,
        new_id,
        question,
        question_normalized,
        answer,
        source_message_id,
        doc_id,
        claims.subject,
        dismissed,
    )
    row = await db.fetchrow(
        "SELECT id, question, answer, source_message_id, doc_id, created_by, created_at, dismissed "
        "FROM training_answers WHERE tenant_id = $1 AND id = $2",
        claims.tenant_id,
        new_id,
    )
    assert row is not None  # noqa: S101 — we just inserted it.
    return TrainingAnswer(
        id=str(row["id"]),
        question=str(row["question"]),
        answer=row["answer"],
        source_message_id=row["source_message_id"],
        doc_id=row["doc_id"],
        created_by=str(row["created_by"]),
        created_at=row["created_at"],
        dismissed=bool(row["dismissed"]),
    )


async def list_taught_question_keys(
    db: Database, claims: AuthClaims, *, limit: int = 500,
) -> set[str]:
    """Return the set of already-taught ``question_normalized`` values for
    this tenant — used to filter the coverage-gaps feed. No pagination: at
    this scale (per-tenant taught-answer count) a flat set is simplest and
    cheapest to filter against in Python; the ``limit`` is a defensive cap.
    """
    _reject_global(claims)

    clamped_limit = max(1, min(limit, 5000))
    rows = await db.fetch(
        "SELECT question_normalized FROM training_answers "
        "WHERE tenant_id = $1 LIMIT $2",
        claims.tenant_id,
        clamped_limit,
    )
    return {str(r["question_normalized"]) for r in rows}
