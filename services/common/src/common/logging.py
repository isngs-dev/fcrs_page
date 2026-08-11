"""Structured JSON logging with request-scoped correlation context.

Log in JSON from day one (KB 02/03/08). A ``correlation_id`` (set by the gateway) plus
``tenant_id`` and ``user_id``/``visitor_id`` are carried in ContextVars and auto-injected
into every line. To avoid leaking secrets/PII, the formatter only emits a curated set of
extra fields — arbitrary attributes attached to a record are dropped.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any

# Request-scoped context. None means "not set" → omitted from the log line.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)

_CONTEXT_VARS: dict[str, ContextVar[str | None]] = {
    "correlation_id": _correlation_id,
    "tenant_id": _tenant_id,
    "user_id": _user_id,
}

# Safe operational fields a caller may attach via ``extra=...``. Anything else is
# dropped so secrets/PII can't accidentally end up in logs.
#
# SR-20 addition: ``lead_id``, ``assigned_agent_id`` and ``reason`` -- all
# opaque ids/short diagnostic strings, never PII -- so
# ``api.leads.assignment.assign_lead_fail_open``'s fail-open warning log
# (D3: "event, tenant_id, lead_id, reason, no PII" is a MANDATORY, tested
# log shape) actually reaches the JSON output instead of being silently
# stripped. ``tenant_id`` itself is unaffected here -- it is already
# auto-injected from the ``_tenant_id`` ContextVar via ``log_context``, not
# from ``extra``.
#
# SR-21 addition: ``kind`` -- the closed-set notification_events vocabulary
# value (e.g. "lead_captured"), never PII -- so
# ``api.notifications.emit.emit_event_safe``'s fail-open warning log
# (D2: "event, tenant_id, kind" is a MANDATORY, tested log shape) actually
# reaches the JSON output instead of being silently stripped.
#
# SR-21 addition: ``deleted_count`` -- an opaque row-count integer emitted by
# ``api.notifications.events_tasks.prune_notification_events`` (D7's periodic
# retention sweep), never PII.
#
# SR-25 addition: ``sort`` and ``direction`` -- route-validated values from
# the closed lead-list sort vocabulary. They make list-query behaviour
# observable without logging a search term or any lead PII.
_ALLOWED_EXTRA = frozenset(
    {
        "endpoint",
        "method",
        "status_code",
        "duration_ms",
        "event",
        "task",
        "attempt",
        "lead_id",
        "assigned_agent_id",
        "reason",
        "kind",
        "deleted_count",
        "sort",
        "direction",
    }
)

# Standard LogRecord attributes (so we know what is an "extra").
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class _SafeLogger(logging.Logger):
    """Logger subclass that strips reserved/non-allowlisted keys from ``extra``
    **before** ``makeRecord`` runs, so stdlib's KeyError can never fire.

    Why a Logger subclass rather than a LoggerAdapter:
    - ``isinstance(get_logger(name), logging.Logger)`` stays True (existing tests
      rely on this and callers may depend on the type).
    - ``logging.getLogger(name)`` returns the same instance when called later
      (the registry owns the object, not us), so caplog-based tests that attach
      a handler by name work without any extra wiring.
    - The fix is surgically close to the crash site (makeRecord), not scattered
      across every adapter wrapper.

    SR-20 addition: also snapshots the current ``_CONTEXT_VARS`` values onto
    the record itself (as ``_chatbot_context``) at **creation** time. Without
    this, ``JsonFormatter.format`` reads the *live* ContextVar at format
    time — correct for the normal case (a StreamHandler formats synchronously
    during ``emit``, microseconds after ``makeRecord``), but wrong whenever a
    record is formatted later, after the emitting scope (e.g. a
    ``with log_context(...):`` block) has already exited and reset the
    ContextVar. This is exactly what happens with pytest's ``caplog``: it
    captures the record but callers often re-format it after the `with`
    block that produced it has closed. Snapshotting at emit time (when the
    context is still live) makes formatting correct regardless of when it's
    later called, with no change to the normal synchronous-handler behavior.
    """

    def makeRecord(
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: tuple[object, ...] | Mapping[str, object],
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | None,
        func: str | None = None,
        extra: Mapping[str, object] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        # Strip any key that is either reserved (already on LogRecord) or not
        # in the explicit allowlist.  This is defense-in-depth: the JsonFormatter
        # allowlist also filters, but filtering here prevents the crash.
        safe_extra: dict[str, object] | None = (
            {k: v for k, v in extra.items() if k in _ALLOWED_EXTRA} if extra else None
        )
        record = super().makeRecord(
            name, level, fn, lno, msg, args, exc_info, func, safe_extra, sinfo
        )
        # Snapshot context now, while it's still live, for correct formatting
        # even if this record is formatted later (e.g. by caplog).
        record._chatbot_context = {  # type: ignore[attr-defined]
            field_name: var.get() for field_name, var in _CONTEXT_VARS.items()
        }
        return record


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Prefer the context snapshot taken at record-creation time
        # (``_SafeLogger.makeRecord``) over a live ContextVar read: the
        # ContextVar may have already been reset (e.g. a ``log_context``
        # block that has since exited) by the time this record is formatted,
        # which happens with caplog-based tests. Records not created via
        # ``_SafeLogger`` (e.g. built directly as ``logging.LogRecord`` in
        # tests) fall back to the live read, preserving existing behavior.
        snapshot = getattr(record, "_chatbot_context", None)
        for name, var in _CONTEXT_VARS.items():
            value = snapshot.get(name) if snapshot is not None else var.get()
            if value is not None:
                payload[name] = value
        for key, value in record.__dict__.items():
            if key in _ALLOWED_EXTRA and key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a ``_SafeLogger`` that emits JSON to stderr. Idempotent.

    Registers ``_SafeLogger`` as the logger class only for the duration of the
    ``getLogger`` call so we don't globally hijack every logger created by
    third-party libraries.
    """
    prev_class = logging.getLoggerClass()
    logging.setLoggerClass(_SafeLogger)
    try:
        logger = logging.getLogger(name)
    finally:
        logging.setLoggerClass(prev_class)
    # If the logger already existed (e.g. root or previously created) and is not
    # a _SafeLogger, upgrade it in-place so the extra-sanitisation still applies.
    if not isinstance(logger, _SafeLogger):
        logger.__class__ = _SafeLogger
    if not any(getattr(h, "_chatbot_json", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._chatbot_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def bind_log_context(
    *,
    correlation_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Token[str | None]]:
    """Set the given context fields (only those provided). Returns reset tokens."""
    updates = {
        "correlation_id": correlation_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
    }
    tokens: dict[str, Token[str | None]] = {}
    for name, value in updates.items():
        if value is not None:
            tokens[name] = _CONTEXT_VARS[name].set(value)
    return tokens


def clear_log_context() -> None:
    """Reset all context fields to unset."""
    for var in _CONTEXT_VARS.values():
        var.set(None)


@contextlib.contextmanager
def log_context(
    *,
    correlation_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> Iterator[None]:
    """Bind context for the duration of the block, restoring prior values on exit."""
    tokens = bind_log_context(
        correlation_id=correlation_id, tenant_id=tenant_id, user_id=user_id
    )
    try:
        yield
    finally:
        for name, token in tokens.items():
            _CONTEXT_VARS[name].reset(token)
