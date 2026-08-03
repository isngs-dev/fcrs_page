"""Unit tests for api.notifications.repository.

Covers:
- enqueue_notification inserts a pending row and returns its job_id
  (tenant-scoped, positional params).
- Re-enqueue with the SAME (tenant_id, dedupe_key) -> ON CONFLICT DO NOTHING
  -> returns None, still ONE row (idempotent front door).
- get_notification_job / mark_notification tenant-scoped.
- Global caller -> ValidationError on every tenant-scoped op.
- Tenant isolation: tenant B never sees/mutates tenant A's job.
- mark_notification's 'sent' flip is guarded by status='pending' (exactly-once).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.notifications.repository import (
    enqueue_notification,
    get_notification_job,
    list_jobs_for_leads,
    mark_notification,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_TENANT_A = "tenant-abc"
_TENANT_B = "tenant-xyz"


def _claims(tenant_id: str = _TENANT_A, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="admin-1", role=role, tenant_id=tenant_id)


class _StubDatabase:
    """In-memory stub database for the notification jobs repository."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}  # job_id -> row
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = query.strip().upper()
        if q.startswith("INSERT INTO NOTIFICATION_JOBS"):
            (
                job_id, tenant_id, channel, template, recipient, subject, body,
                payload, dedupe_key, lead_id,
            ) = args
            for row in self._jobs.values():
                if row["tenant_id"] == tenant_id and row["dedupe_key"] == dedupe_key:
                    return None  # ON CONFLICT DO NOTHING -> RETURNING empty
            self._jobs[job_id] = {
                "job_id": job_id, "tenant_id": tenant_id, "channel": channel,
                "template": template, "recipient": recipient, "subject": subject,
                "body": body, "payload": payload, "dedupe_key": dedupe_key,
                "status": "pending", "attempts": 0, "delivery_ref": None,
                "last_error": None, "created_at": _NOW, "updated_at": _NOW,
                "lead_id": lead_id,
            }
            return job_id
        if q.startswith("SELECT JOB_ID FROM NOTIFICATION_JOBS"):
            tenant_id, dedupe_key = args
            for row in self._jobs.values():
                if row["tenant_id"] == tenant_id and row["dedupe_key"] == dedupe_key:
                    return row["job_id"]
            return None
        return None

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM NOTIFICATION_JOBS" in query.upper():
            tenant_id, job_id = args
            row = self._jobs.get(job_id)
            if row is None or row["tenant_id"] != tenant_id:
                return None
            return row
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        q = query.strip().upper()
        if q.startswith("UPDATE NOTIFICATION_JOBS"):
            if "STATUS = 'PENDING'" in q:
                tenant_id, job_id, status, delivery_ref = args
                row = self._jobs.get(job_id)
                if row is None or row["tenant_id"] != tenant_id or row["status"] != "pending":
                    return "UPDATE 0"
                row["status"] = status
                row["delivery_ref"] = delivery_ref
                if "ATTEMPTS + 1" in q:
                    row["attempts"] += 1
                return "UPDATE 1"
            tenant_id, job_id, status, last_error = args
            row = self._jobs.get(job_id)
            if row is None or row["tenant_id"] != tenant_id:
                return "UPDATE 0"
            row["status"] = status
            row["last_error"] = last_error
            if "ATTEMPTS + 1" in q:
                row["attempts"] += 1
            return "UPDATE 1"
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        q = query.strip().upper()
        if "FROM NOTIFICATION_JOBS" in q and "LEAD_ID = ANY" in q:
            tenant_id, lead_ids = args[0], args[1]
            rows = [
                row for row in self._jobs.values()
                if row["tenant_id"] == tenant_id and row.get("lead_id") in lead_ids
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return rows
        return []

    async def close(self) -> None:
        pass


# ==============================================================================
# enqueue_notification -- idempotency
# ==============================================================================


async def test_enqueue_inserts_pending_row_and_returns_job_id() -> None:
    db = _StubDatabase()
    job_id = await enqueue_notification(
        db, _claims(), channel="email", recipient="alice@example.com",
        subject="hi", body="hello", dedupe_key="test:1",
    )
    assert job_id is not None
    assert len(db._jobs) == 1
    assert db._jobs[job_id]["status"] == "pending"


async def test_reenqueue_same_dedupe_key_returns_none_and_no_duplicate_row() -> None:
    db = _StubDatabase()
    claims = _claims()
    job_id_1 = await enqueue_notification(
        db, claims, channel="email", recipient="alice@example.com",
        subject="hi", body="hello", dedupe_key="test:same",
    )
    job_id_2 = await enqueue_notification(
        db, claims, channel="email", recipient="alice@example.com",
        subject="hi again", body="hello again", dedupe_key="test:same",
    )
    assert job_id_1 is not None
    assert job_id_2 is None
    assert len(db._jobs) == 1


async def test_different_tenants_can_use_same_dedupe_key() -> None:
    db = _StubDatabase()
    job_id_a = await enqueue_notification(
        db, _claims(_TENANT_A), channel="email", recipient="a@example.com",
        subject="hi", body="hello", dedupe_key="test:shared",
    )
    job_id_b = await enqueue_notification(
        db, _claims(_TENANT_B), channel="email", recipient="b@example.com",
        subject="hi", body="hello", dedupe_key="test:shared",
    )
    assert job_id_a is not None
    assert job_id_b is not None
    assert job_id_a != job_id_b
    assert len(db._jobs) == 2


# ==============================================================================
# get_notification_job / mark_notification
# ==============================================================================


async def test_get_and_mark_notification() -> None:
    db = _StubDatabase()
    claims = _claims()
    job_id = await enqueue_notification(
        db, claims, channel="email", recipient="a@example.com",
        subject="hi", body="hello", dedupe_key="test:mark",
    )
    assert job_id is not None

    job = await get_notification_job(db, claims, job_id)
    assert job is not None
    assert job.status == "pending"

    await mark_notification(
        db, claims, job_id, status="sent", delivery_ref="ref-1", increment_attempt=True
    )

    job = await get_notification_job(db, claims, job_id)
    assert job is not None
    assert job.status == "sent"
    assert job.delivery_ref == "ref-1"
    assert job.attempts == 1


async def test_mark_sent_guarded_by_pending_status_exactly_once() -> None:
    db = _StubDatabase()
    claims = _claims()
    job_id = await enqueue_notification(
        db, claims, channel="email", recipient="a@example.com",
        subject="hi", body="hello", dedupe_key="test:once",
    )
    assert job_id is not None

    await mark_notification(
        db, claims, job_id, status="sent", delivery_ref="ref-1", increment_attempt=True
    )
    # Second flip attempt: row is no longer 'pending' -> no-op, no double increment.
    await mark_notification(
        db, claims, job_id, status="sent", delivery_ref="ref-2", increment_attempt=True
    )

    job = await get_notification_job(db, claims, job_id)
    assert job is not None
    assert job.delivery_ref == "ref-1"  # unchanged by the second (no-op) flip
    assert job.attempts == 1


async def test_get_missing_job_returns_none() -> None:
    db = _StubDatabase()
    job = await get_notification_job(db, _claims(), "does-not-exist")
    assert job is None


# ==============================================================================
# Tenant isolation
# ==============================================================================


async def test_cross_tenant_get_returns_none() -> None:
    db = _StubDatabase()
    job_id = await enqueue_notification(
        db, _claims(_TENANT_A), channel="email", recipient="a@example.com",
        subject="hi", body="hello", dedupe_key="test:iso",
    )
    assert job_id is not None

    job = await get_notification_job(db, _claims(_TENANT_B), job_id)
    assert job is None


async def test_cross_tenant_mark_does_not_mutate() -> None:
    db = _StubDatabase()
    job_id = await enqueue_notification(
        db, _claims(_TENANT_A), channel="email", recipient="a@example.com",
        subject="hi", body="hello", dedupe_key="test:iso2",
    )
    assert job_id is not None

    await mark_notification(db, _claims(_TENANT_B), job_id, status="failed", last_error="nope")

    job = await get_notification_job(db, _claims(_TENANT_A), job_id)
    assert job is not None
    assert job.status == "pending"


# ==============================================================================
# Global caller rejection
# ==============================================================================


# ==============================================================================
# lead_id write-path + list_jobs_for_leads (SR-9.3 D4)
# ==============================================================================


async def test_enqueue_persists_lead_id_when_supplied() -> None:
    db = _StubDatabase()
    job_id = await enqueue_notification(
        db, _claims(), channel="email", recipient="a@example.com",
        subject="hi", body="hello", dedupe_key="test:lead1", lead_id="lead-1",
    )
    assert job_id is not None
    assert db._jobs[job_id]["lead_id"] == "lead-1"


async def test_enqueue_persists_null_lead_id_when_omitted() -> None:
    db = _StubDatabase()
    job_id = await enqueue_notification(
        db, _claims(), channel="email", recipient="a@example.com",
        subject="hi", body="hello", dedupe_key="test:lead2",
    )
    assert job_id is not None
    assert db._jobs[job_id]["lead_id"] is None


async def test_list_jobs_for_leads_tenant_isolation_same_lead_id_string() -> None:
    """The single highest-value test in this sprint (J4/D4): notification_jobs
    has no composite FK, so this join's safety rests entirely on the query
    predicate. Seed the SAME lead_id string in two tenants and confirm each
    tenant's list_jobs_for_leads only ever returns its own job."""
    db = _StubDatabase()
    shared_lead_id = "lead-shared-123"

    job_a = await enqueue_notification(
        db, _claims(_TENANT_A), channel="email", recipient="a@example.com",
        subject="A's notification", body="hello a", dedupe_key="test:shared-lead-a",
        lead_id=shared_lead_id,
    )
    job_b = await enqueue_notification(
        db, _claims(_TENANT_B), channel="email", recipient="b@example.com",
        subject="B's notification", body="hello b", dedupe_key="test:shared-lead-b",
        lead_id=shared_lead_id,
    )
    assert job_a is not None
    assert job_b is not None

    jobs_a = await list_jobs_for_leads(db, _claims(_TENANT_A), lead_ids=[shared_lead_id])
    jobs_b = await list_jobs_for_leads(db, _claims(_TENANT_B), lead_ids=[shared_lead_id])

    assert [j.job_id for j in jobs_a] == [job_a]
    assert [j.job_id for j in jobs_b] == [job_b]
    assert jobs_a[0].subject == "A's notification"
    assert jobs_b[0].subject == "B's notification"


async def test_list_jobs_for_leads_empty_list_returns_empty_no_query() -> None:
    db = _StubDatabase()
    jobs = await list_jobs_for_leads(db, _claims(), lead_ids=[])
    assert jobs == []


async def test_list_jobs_for_leads_global_caller_rejected() -> None:
    db = _StubDatabase()
    global_claims = AuthClaims(subject="root", role=Role.PLATFORM_ADMIN, tenant_id=None)
    with pytest.raises(ValidationError):
        await list_jobs_for_leads(db, global_claims, lead_ids=["lead-1"])


async def test_global_caller_rejected_on_every_op() -> None:
    db = _StubDatabase()
    global_claims = AuthClaims(subject="root", role=Role.PLATFORM_ADMIN, tenant_id=None)

    with pytest.raises(ValidationError):
        await enqueue_notification(
            db, global_claims, channel="email", recipient="a@example.com",
            subject="hi", body="hello", dedupe_key="test:global",
        )

    with pytest.raises(ValidationError):
        await get_notification_job(db, global_claims, "any-id")

    with pytest.raises(ValidationError):
        await mark_notification(db, global_claims, "any-id", status="failed")
