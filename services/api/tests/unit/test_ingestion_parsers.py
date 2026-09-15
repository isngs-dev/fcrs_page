"""Unit tests for api.ingestion.parsers.

Covers:
- text/plain bytes → normalized text (whitespace normalization).
- A small docx fixture (built in-test with python-docx) → extracted paragraph text.
- Unknown content type → ValidationError(UNSUPPORTED_CONTENT_TYPE).
- MIME parameter stripping ("text/plain; charset=utf-8" still dispatches correctly).
- Empty/binary garbage declared as text/plain → no crash, returns a string.
- Binary garbage declared as docx → ValidationError(PARSE_ERROR).
"""
from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

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

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _reset_modules() -> None:
    # Reimport ingestion modules fresh + clear settings caches. Do NOT delete
    # api.config: that splits the module graph (api.app stays bound to the
    # original config) and poisons later tests. Clearing the caches on the single
    # shared config module gives fresh settings safely.
    for key in list(sys.modules.keys()):
        if key.startswith("api.ingestion"):
            del sys.modules[key]
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _build_docx(paragraphs: list[str]) -> bytes:
    """Build a real .docx file in-memory using python-docx."""
    from docx import Document  # type: ignore[import-untyped]

    doc = Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ==============================================================================
# text/plain
# ==============================================================================


def test_parse_text_plain_basic() -> None:
    """text/plain bytes are decoded and returned as a string."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        result = parse("text/plain", b"Hello world")
        assert result == "Hello world"


def test_parse_text_plain_whitespace_normalization() -> None:
    """Multiple spaces on a line are collapsed; leading/trailing stripped."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        data = b"  hello   world  \n  foo   bar  "
        result = parse("text/plain", data)
        assert "  " not in result, "Multiple spaces should be collapsed"
        assert result == "hello world\nfoo bar"


def test_parse_text_plain_strips_mime_parameters() -> None:
    """Content-Type with parameters still dispatches to the txt parser."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        result = parse("text/plain; charset=utf-8", b"param test")
        assert result == "param test"


def test_parse_text_plain_replace_invalid_utf8() -> None:
    """Invalid UTF-8 bytes are replaced rather than raising."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        result = parse("text/plain", b"ok \xff\xfe bad")
        # Should return a string without raising.
        assert isinstance(result, str)


def test_parse_text_plain_empty_bytes() -> None:
    """Empty bytes under text/plain → empty string (no crash)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        result = parse("text/plain", b"")
        assert result == ""


# ==============================================================================
# docx
# ==============================================================================


def test_parse_docx_extracts_paragraphs() -> None:
    """A real docx fixture (built with python-docx) → paragraph text extracted."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        docx_bytes = _build_docx(["First paragraph.", "Second paragraph."])
        result = parse(_DOCX_MIME, docx_bytes)
        assert "First paragraph." in result
        assert "Second paragraph." in result


def test_parse_docx_single_paragraph() -> None:
    """Single-paragraph docx returns the paragraph text."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        docx_bytes = _build_docx(["Only paragraph."])
        result = parse(_DOCX_MIME, docx_bytes)
        assert "Only paragraph." in result


def test_parse_docx_garbage_raises_parse_error() -> None:
    """Binary garbage declared as docx raises ValidationError(PARSE_ERROR)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.parsers import parse

        with pytest.raises(ValidationError) as exc_info:
            parse(_DOCX_MIME, b"\x00\x01\x02\x03 not a docx file")
        assert exc_info.value.code == "PARSE_ERROR"


# ==============================================================================
# text/html (add-a-website feature)
# ==============================================================================


def test_parse_html_extracts_visible_text() -> None:
    """Basic HTML: visible text in the body is extracted."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        html = b"<html><body><h1>Welcome</h1><p>We sell widgets.</p></body></html>"
        result = parse("text/html", html)
        assert "Welcome" in result
        assert "We sell widgets." in result


def test_parse_html_strips_boilerplate_tags() -> None:
    """script/style/nav/header/footer/aside/form/noscript content is dropped
    entirely, not just their tags -- so nav links and inline scripts never
    pollute what gets chunked/embedded."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        html = (
            b"<html><body>"
            b"<nav>Home | About | Contact</nav>"
            b"<script>var x = 'tracking-pixel-id';</script>"
            b"<style>.foo { color: red; }</style>"
            b"<header>Site Header</header>"
            b"<main><p>Actual page content.</p></main>"
            b"<footer>Copyright 2026</footer>"
            b"<aside>Related links</aside>"
            b"<form><input name='email'></form>"
            b"<noscript>Enable JS</noscript>"
            b"</body></html>"
        )
        result = parse("text/html", html)
        assert "Actual page content." in result
        for boilerplate in (
            "Home | About | Contact",
            "tracking-pixel-id",
            "Site Header",
            "Copyright 2026",
            "Related links",
            "Enable JS",
        ):
            assert boilerplate not in result


def test_parse_html_normalizes_whitespace() -> None:
    """Multiple spaces/blank lines collapse, same as _parse_text_plain."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        html = b"<html><body><p>hello   world</p>\n\n\n\n<p>foo</p></body></html>"
        result = parse("text/html", html)
        assert "  " not in result
        assert result.count("\n\n\n") == 0


def test_parse_html_empty_page_returns_empty_string() -> None:
    """An HTML page with no visible text -> empty string, no crash."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        result = parse("text/html", b"<html><head></head><body></body></html>")
        assert result == ""


def test_parse_html_tolerates_malformed_markup() -> None:
    """Unclosed tags / malformed HTML don't crash (html.parser is lenient)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.parsers import parse

        result = parse("text/html", b"<html><body><p>Unclosed paragraph<div>Nested</html>")
        assert "Unclosed paragraph" in result
        assert "Nested" in result


# ==============================================================================
# Unknown content type
# ==============================================================================


def test_parse_unknown_type_raises_unsupported() -> None:
    """An unknown content type raises ValidationError(UNSUPPORTED_CONTENT_TYPE)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.parsers import parse

        with pytest.raises(ValidationError) as exc_info:
            parse("image/png", b"\x89PNG\r\n")
        assert exc_info.value.code == "UNSUPPORTED_CONTENT_TYPE"


def test_parse_pdf_raises_unsupported() -> None:
    """application/pdf is deferred to S5.2b and must raise UNSUPPORTED_CONTENT_TYPE."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from common.errors import ValidationError

        from api.ingestion.parsers import parse

        with pytest.raises(ValidationError) as exc_info:
            parse("application/pdf", b"%PDF-1.4")
        assert exc_info.value.code == "UNSUPPORTED_CONTENT_TYPE"
