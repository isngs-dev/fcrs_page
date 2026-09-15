"""Document parsers for the ingestion pipeline.

``parse(content_type, data)`` is the single entry point. It dispatches to a
per-format handler based on the MIME content type and returns the normalized
plain text. Unsupported types raise ``ValidationError`` (code
``UNSUPPORTED_CONTENT_TYPE``) — never silently return empty/fake text.

Supported content types (S5.2 scope):
- ``text/plain``      → UTF-8 decode (errors="replace") + whitespace normalization.
- docx MIME          → python-docx paragraph extraction, joined with newlines.
- ``text/html``        → BeautifulSoup boilerplate-strip + visible-text extraction
  (add-a-website feature) -- see ``_parse_html``.

PDF + OCR are deferred to S5.2b.
"""
from __future__ import annotations

import re

from common.errors import ValidationError

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Tags whose text is never part of a page's actual content -- scripts,
# styles, and chrome (nav/header/footer/aside/forms), stripped before text
# extraction so they don't pollute what gets embedded.
_HTML_BOILERPLATE_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")


def _normalize_whitespace(text: str) -> str:
    """Collapse horizontal whitespace per line, strip each line, collapse
    runs of blank lines, strip overall leading/trailing whitespace.

    Shared by ``_parse_text_plain`` and ``_parse_html`` -- both produce
    "a block of prose", just extracted differently.
    """
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    normalized = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return normalized.strip()


def _parse_text_plain(data: bytes) -> str:
    """Decode bytes as UTF-8 (replacing undecodable sequences) and normalize whitespace."""
    text = data.decode("utf-8", errors="replace")
    return _normalize_whitespace(text)


def _parse_html(data: bytes) -> str:
    """Extract visible text from an HTML page (add-a-website feature).

    Strips ``_HTML_BOILERPLATE_TAGS`` (script/style/nav/header/footer/aside/
    form/noscript) before extracting text, so navigation chrome and inline
    scripts never pollute what gets chunked/embedded. Uses BeautifulSoup's
    built-in ``html.parser`` backend -- no ``lxml`` dependency needed, and
    it's lenient enough for the malformed real-world HTML this will see.
    An empty or unparseable page raises ``ValidationError`` (``PARSE_ERROR``),
    matching ``_parse_docx``'s failure shape, rather than returning silently
    empty text.
    """
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415 — deferred import cost
    except ImportError as exc:
        raise ValidationError(
            "beautifulsoup4 is required to parse HTML pages but is not installed.",
            code="PARSE_ERROR",
        ) from exc

    try:
        soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "html.parser")
        for tag in soup(_HTML_BOILERPLATE_TAGS):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception as exc:
        raise ValidationError(
            f"Failed to parse HTML page: {exc}",
            code="PARSE_ERROR",
        ) from exc

    return _normalize_whitespace(text)


def _parse_docx(data: bytes) -> str:
    """Extract paragraph text from a .docx file using python-docx.

    Joins non-empty paragraphs with newlines. An empty or invalid file
    raises ``ValidationError`` (``PARSE_ERROR``) rather than crashing silently.
    """
    import io  # noqa: PLC0415 — deferred to avoid import cost when not used

    try:
        from docx import Document  # noqa: PLC0415
    except ImportError as exc:
        raise ValidationError(
            "python-docx is required to parse .docx files but is not installed.",
            code="PARSE_ERROR",
        ) from exc

    try:
        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    except Exception as exc:
        raise ValidationError(
            f"Failed to parse docx document: {exc}",
            code="PARSE_ERROR",
        ) from exc

    return "\n".join(paragraphs)


def parse(content_type: str, data: bytes) -> str:
    """Dispatch to the correct parser for ``content_type`` and return normalized text.

    Parameters
    ----------
    content_type:
        The MIME type of the uploaded file (validated by the route before calling
        this function, but we re-check here to be safe).
    data:
        The raw file bytes.

    Returns
    -------
    str
        Normalized plain text. Never empty on a valid non-empty file (but an
        empty file may return an empty string — callers should record that).

    Raises
    ------
    ValidationError(code="UNSUPPORTED_CONTENT_TYPE")
        When ``content_type`` is not recognised by any parser.
    ValidationError(code="PARSE_ERROR")
        When the file bytes cannot be parsed by the declared type's handler.
    """
    # Strip parameters (e.g. "text/plain; charset=utf-8" → "text/plain").
    mime = content_type.split(";")[0].strip().lower()

    if mime == "text/plain":
        return _parse_text_plain(data)
    if mime == _DOCX_MIME:
        return _parse_docx(data)
    if mime == "text/html":
        return _parse_html(data)

    _supported = (
        "text/plain, "
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document, "
        "text/html"
    )
    raise ValidationError(
        f"Unsupported content type: {content_type!r}. Supported: {_supported}.",
        code="UNSUPPORTED_CONTENT_TYPE",
    )
