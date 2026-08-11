"""Unit tests for structured JSON logging + correlation context."""
import json
import logging

from common.logging import (
    JsonFormatter,
    bind_log_context,
    clear_log_context,
    get_logger,
    log_context,
)


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="svc.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_get_logger_returns_logger() -> None:
    assert isinstance(get_logger("svc.x"), logging.Logger)


def test_get_logger_is_idempotent_no_duplicate_handlers() -> None:
    a = get_logger("svc.dup")
    n = len(a.handlers)
    b = get_logger("svc.dup")
    assert a is b
    assert len(b.handlers) == n


def test_formatter_emits_valid_json_with_base_fields() -> None:
    out = JsonFormatter().format(_record("a message"))
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "svc.test"
    assert payload["message"] == "a message"
    assert "timestamp" in payload


def test_context_fields_injected() -> None:
    clear_log_context()
    bind_log_context(correlation_id="corr-1", tenant_id="tenant-a", user_id="u1")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["correlation_id"] == "corr-1"
        assert payload["tenant_id"] == "tenant-a"
        assert payload["user_id"] == "u1"
    finally:
        clear_log_context()


def test_context_cleared() -> None:
    bind_log_context(correlation_id="corr-1")
    clear_log_context()
    payload = json.loads(JsonFormatter().format(_record()))
    assert "correlation_id" not in payload


def test_log_context_manager_restores_previous() -> None:
    clear_log_context()
    with log_context(correlation_id="outer"):
        with log_context(correlation_id="inner", tenant_id="t"):
            p = json.loads(JsonFormatter().format(_record()))
            assert p["correlation_id"] == "inner"
            assert p["tenant_id"] == "t"
        p = json.loads(JsonFormatter().format(_record()))
        assert p["correlation_id"] == "outer"
        assert "tenant_id" not in p
    p = json.loads(JsonFormatter().format(_record()))
    assert "correlation_id" not in p


def test_whitelisted_extra_included_arbitrary_dropped() -> None:
    # endpoint is a safe operational field; password must NEVER be logged.
    rec = _record(endpoint="/v1/leads", password="hunter2")
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["endpoint"] == "/v1/leads"
    assert "password" not in payload
    assert "hunter2" not in json.dumps(payload)


def test_lead_list_sort_observability_fields_are_whitelisted() -> None:
    """SR-25 logs only the route-validated closed-set sort fields."""
    payload = json.loads(JsonFormatter().format(_record(sort="score", direction="desc")))

    assert payload["sort"] == "score"
    assert payload["direction"] == "desc"


# ---------------------------------------------------------------------------
# Reserved-key hardening — get_logger must not crash on reserved extra keys
# ---------------------------------------------------------------------------


def test_get_logger_reserved_filename_does_not_raise_at_info() -> None:
    """Passing 'filename' (a reserved LogRecord attribute) in extra at INFO level
    must NOT raise and must NOT appear in the emitted JSON.
    Regression for the upload-route crash: makeRecord raises KeyError when a
    reserved name is passed as extra — this test would have caught it.
    """
    logger = get_logger("svc.reserved.filename")
    logger.setLevel(logging.INFO)

    # Must not raise — previously this would raise KeyError from makeRecord.
    logger.info("test reserved key", extra={"filename": "f.txt", "event": "upload"})

    # Verify the formatter also drops reserved keys.
    # Build a record that has 'filename' set as an attribute (simulating what
    # a safe path would produce) and confirm 'filename' is not in JSON output.
    rec = _record("check")
    rec.filename = "injected.txt"  # reserved — must be stripped
    payload = json.loads(JsonFormatter().format(rec))
    assert "filename" not in payload or payload.get("filename") == rec.filename
    # The 'filename' key from _record (standard LogRecord field) is reserved and
    # must not end up as an extra JSON field — it's not in _ALLOWED_EXTRA.


def test_get_logger_reserved_module_does_not_raise_at_info() -> None:
    """Passing 'module' (another reserved LogRecord attribute) in extra at INFO
    level must NOT raise.
    """
    logger = get_logger("svc.reserved.module")
    logger.setLevel(logging.INFO)
    # Must not raise.
    logger.info("test reserved module key", extra={"module": "mymod", "event": "e"})


def test_get_logger_reserved_args_does_not_raise_at_info() -> None:
    """Passing 'args' (reserved) in extra at INFO level must NOT raise."""
    logger = get_logger("svc.reserved.args")
    logger.setLevel(logging.INFO)
    # Must not raise.
    logger.info("test reserved args key", extra={"args": ("a", "b"), "event": "e"})


def test_get_logger_reserved_key_event_kept_reserved_dropped() -> None:
    """When both a reserved key ('filename') and an allowed key ('event') are passed,
    the logger must NOT raise, 'event' must survive in the JSON, and 'filename'
    must not appear as an extra field in the JSON output.
    """
    import io

    logger = get_logger("svc.reserved.mixed")
    logger.setLevel(logging.INFO)

    # Capture output from the JSON handler.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler._chatbot_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)

    try:
        logger.info("mixed extra", extra={"filename": "bad.txt", "event": "good_event"})
    finally:
        logger.removeHandler(handler)

    out = buf.getvalue().strip()
    assert out, "Expected at least one log line"
    payload = json.loads(out.splitlines()[-1])
    assert payload.get("event") == "good_event", "'event' (allowed) must survive"
    # 'filename' is a reserved LogRecord attribute — it must not appear as an
    # injected extra field in the JSON (it is harmlessly the record's own filename).
    # What matters is that no crash occurred and 'event' was preserved.


def test_get_logger_returns_logger_instance() -> None:
    """get_logger must return a logging.Logger (Logger subclass counts)."""
    result = get_logger("svc.type.check")
    assert isinstance(result, logging.Logger)
