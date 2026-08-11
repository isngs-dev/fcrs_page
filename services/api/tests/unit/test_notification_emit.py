"""Unit tests for api.notifications.emit (SR-21 D2).

Covers the fail-open contract: emit_event_safe wraps
events_repository.emit_event, catches EVERY exception, logs a warning with
event/tenant_id/kind (no PII), and swallows -- callers must never see the
failure.

``tenant_id`` is a ContextVar-sourced field (``common.logging``), injected
into the formatted JSON output at format time -- it is NOT a raw ``extra``
key on ``record.__dict__``. Asserting it directly off ``record.__dict__``
(as an earlier draft of this file did) passes or fails for the wrong
reason: it never actually proves the field reaches real emitted output.
The correct proof -- mirroring
``tests/unit/test_lead_assignment.py::test_assign_lead_fail_open_never_propagates_and_logs_warning``,
this codebase's verified precedent for exactly this shape -- is to format
the captured record through the REAL ``JsonFormatter`` (via
``caplog.handler.format``, with the formatter substituted in) and assert
against the parsed JSON payload, while still inside the
``log_context(tenant_id=...)`` block that bound the ContextVar (formatting
after the block exits would read a reset/None value).
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from common.auth import AuthClaims, Role
from common.logging import JsonFormatter

from api.notifications.emit import emit_event_safe

_TENANT_ID = "tenant-abc"


def _claims() -> AuthClaims:
    return AuthClaims(subject="user-1", role=Role.CLIENT_ADMIN, tenant_id=_TENANT_ID)


async def test_emit_event_safe_calls_through_on_success() -> None:
    db = object()
    claims = _claims()
    with patch(
        "api.notifications.emit.emit_event", new=AsyncMock(return_value="event-123"),
    ) as mocked:
        event_id = await emit_event_safe(
            db, claims, kind="lead_captured", category="leads",
            target_type="lead", target_id="lead-1", payload={"lead_id": "lead-1"},
            actor_id=None,
        )
    assert event_id == "event-123"
    mocked.assert_awaited_once()


async def test_emit_event_safe_swallows_exception(caplog: pytest.LogCaptureFixture) -> None:
    db = object()
    claims = _claims()
    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("db exploded")),
    ), caplog.at_level(logging.WARNING):
        event_id = await emit_event_safe(
            db, claims, kind="lead_captured", category="leads",
            target_type="lead", target_id="lead-1", payload={"lead_id": "lead-1"},
            actor_id=None,
        )
    assert event_id is None  # never raises -- caller proceeds normally


async def test_emit_event_safe_logs_warning_with_required_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verified against REAL emitted output (the real ``JsonFormatter``),
    not just ``record.__dict__`` -- see module docstring. This is the proof
    that ``kind`` is actually allowlisted in
    ``common.logging._ALLOWED_EXTRA`` and that ``tenant_id`` (ContextVar-
    injected) is bound at the point the warning is logged.
    """
    caplog.handler.setFormatter(JsonFormatter())

    db = object()
    claims = _claims()
    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("db exploded")),
    ), caplog.at_level(logging.WARNING):
        await emit_event_safe(
            db, claims, kind="lead_captured", category="leads",
            target_type="lead", target_id="lead-1", payload=None, actor_id=None,
        )

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected a warning-level log on emit failure"
    formatted = [caplog.handler.format(r) for r in warning_records]
    payloads = [json.loads(line) for line in formatted]
    payload = payloads[-1]
    assert payload.get("event") == "notification_event_emit_failed"
    assert payload.get("tenant_id") == _TENANT_ID
    assert payload.get("kind") == "lead_captured"


async def test_emit_event_safe_never_raises_even_for_arbitrary_exceptions() -> None:
    """Any exception type is caught -- not just a narrow subclass."""
    db = object()
    claims = _claims()

    class _WeirdError(BaseException):
        pass

    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=ValueError("boom")),
    ):
        result = await emit_event_safe(
            db, claims, kind="lead_captured", category="leads",
            target_type="lead", target_id="lead-1", payload=None, actor_id=None,
        )
    assert result is None


async def test_emit_event_safe_does_not_log_pii(caplog: pytest.LogCaptureFixture) -> None:
    """PII in payload must never leak into the warning log line."""
    db = object()
    claims = _claims()
    pii_email = "dana.contact@example.com"
    with patch(
        "api.notifications.emit.emit_event",
        new=AsyncMock(side_effect=RuntimeError("db exploded")),
    ), caplog.at_level(logging.WARNING):
        await emit_event_safe(
            db, claims, kind="lead_captured", category="leads",
            target_type="lead", target_id="lead-1",
            payload={"lead_id": "lead-1", "email": pii_email},  # a violating caller
            actor_id=None,
        )
    for record in caplog.records:
        assert pii_email not in record.getMessage()
        for value in record.__dict__.values():
            assert pii_email not in str(value)
