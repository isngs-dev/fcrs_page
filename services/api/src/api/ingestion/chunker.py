"""Sentence-aware deterministic text chunker.

``chunk_text(text, *, max_chars, overlap)`` splits *text* into a list of strings
where:

- Each chunk is at most *max_chars* characters long (except when a single token
  longer than *max_chars* is encountered — it is hard-split, never dropped).
- The last *overlap* characters of chunk N are prepended to chunk N+1 (trailing
  context window so downstream embedding models see sentence-boundary context).
- Markdown ATX headings (``#`` through ``######``) are treated as block
  boundaries: a heading line plus everything under it up to the next heading
  is one candidate packing unit, kept together WHOLE whenever it fits within
  *max_chars* — a heading is never separated from its own content just
  because the sentence-level packer would otherwise split partway through
  (e.g. an FAQ entry's question variants ending up in one chunk and its
  answer variants in the next, with no heading text in either to anchor a
  keyword/vector match back to the topic). A block larger than *max_chars*
  falls back to the sentence-level packing below, unchanged. Text with no
  ATX headings at all chunks exactly as before this behavior was added.
- Splitting prefers sentence boundaries (``re`` sentence-end detection). When a
  sentence fits entirely in the remaining budget, it is packed into the current
  chunk; when it would overflow, the current chunk is emitted first.
- Empty / whitespace-only input returns ``[]`` (no error, no fabricated content).
- The function is **pure and deterministic**: identical input always yields
  identical output, so re-ingesting the same document is stable.

This module has **no database or I/O side effects**.  It is intentionally
dependency-free (stdlib only) so it can be tested without any infrastructure.
"""
from __future__ import annotations

import re

# Sentence-boundary split pattern.
# We match on ". " / "! " / "? " sequences (punctuation followed by whitespace),
# consuming the whitespace in the split (re.split drops the matched separator).
# Two patterns cover plain endings and quote-terminated endings:
#   r'(?<=[.!?])\s+'          — e.g. "dog. Next"
#   r'(?<=[.!?]["\'])\s+'     — e.g. 'said." Next'
# Python's fixed-width lookbehind disallows alternation with different widths
# inside ONE lookbehind, so we use a compiled alternation at match level instead.
_SENTENCE_END_RE = re.compile(r'(?<=[.!?])\s+|(?<=[.!?]["\'])\s+')

# Markdown ATX heading line: 1-6 leading '#' then whitespace, anchored to the
# start of a line (MULTILINE). Matches "### **14\. What is...**" as found in
# FAQ-style knowledge docs (the backslash before the period is literal
# escaped-markdown text, not part of this pattern).
_HEADING_RE = re.compile(r'^#{1,6}\s+.*$', re.MULTILINE)


def chunk_text(text: str, *, max_chars: int, overlap: int) -> list[str]:
    """Split *text* into overlapping sentence-aware chunks.

    Parameters
    ----------
    text:
        Input text to chunk.  Empty / whitespace-only → ``[]``.
    max_chars:
        Maximum characters per chunk.  A sentence that is itself longer than
        *max_chars* is hard-split at exactly *max_chars* boundaries.
    overlap:
        Number of trailing characters from the previous chunk prepended to the
        next chunk (trailing context).  ``0`` → no overlap.

    Returns
    -------
    list[str]
        Ordered list of chunk strings.  Content is never dropped.
    """
    if not text or not text.strip():
        return []

    # Split on markdown heading boundaries first; each block that fits within
    # max_chars is packed as ONE atomic unit (never split at a sentence
    # boundary inside it). A block too large for max_chars, or text with no
    # headings at all (a single "block" = the whole text), falls back to
    # sentence-level splitting below — identical to this function's behavior
    # before heading-awareness was added.
    blocks = _split_heading_blocks(text.strip())

    chunks: list[str] = []
    # `current` is the text accumulated into the chunk being built.
    # `budget` is how many chars are still available in `current`.
    current = ""

    def _emit() -> None:
        """Flush `current` as a completed chunk."""
        nonlocal current
        if current.strip():
            chunks.append(current)
        current = ""

    def _start_next(prev_chunk: str) -> None:
        """Start a new accumulator seeded with the overlap tail of *prev_chunk*."""
        nonlocal current
        if overlap > 0 and prev_chunk:
            tail = prev_chunk[-overlap:]
            current = tail
        else:
            current = ""

    for block in blocks:
        # A block that fits whole is packed as a single atomic "sentence" --
        # never split at a sentence boundary inside it. A block too large for
        # max_chars degrades to ordinary sentence-level splitting.
        sentences = [block] if len(block) <= max_chars else _split_sentences(block)

        for sentence in sentences:
            # A single sentence that already exceeds max_chars must be hard-split
            # into max_chars pieces before we pack them.
            pieces = _hard_split(sentence, max_chars) if len(sentence) > max_chars else [sentence]

            for piece in pieces:
                separator = " " if current else ""
                candidate = current + separator + piece

                if len(candidate) <= max_chars:
                    current = candidate
                else:
                    # Emit whatever we have so far, then start fresh with overlap.
                    prev = current
                    _emit()
                    _start_next(prev)

                    # Now try to fit the piece into the new (overlap-seeded) current.
                    separator2 = " " if current else ""
                    candidate2 = current + separator2 + piece
                    if len(candidate2) <= max_chars:
                        current = candidate2
                    else:
                        # Even with just the overlap prefix + piece it's too long.
                        # Hard-split the piece into max_chars windows, carrying the
                        # overlap prefix only for the very first sub-piece.
                        prefix = current
                        sub_pieces = _hard_split(piece, max_chars)
                        for j, sp in enumerate(sub_pieces):
                            if j == 0 and prefix:
                                sep = " " if prefix else ""
                                combined = prefix + sep + sp
                                if len(combined) <= max_chars:
                                    current = combined
                                else:
                                    # prefix alone too long or combined too long → emit prefix first
                                    prev2 = prefix
                                    if prev2.strip():
                                        chunks.append(prev2)
                                    current = sp
                            else:
                                sep = " " if current else ""
                                cand = current + sep + sp
                                if len(cand) <= max_chars:
                                    current = cand
                                else:
                                    prev3 = current
                                    _emit()
                                    _start_next(prev3)
                                    sep2 = " " if current else ""
                                    current = (current + sep2 + sp).lstrip()

    if current.strip():
        chunks.append(current)

    return chunks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Split *text* on sentence boundaries, returning non-empty sentence strings."""
    parts = _SENTENCE_END_RE.split(text)
    return [p for p in parts if p.strip()]


def _split_heading_blocks(text: str) -> list[str]:
    """Split *text* into blocks at markdown ATX heading boundaries.

    Each block starts at a heading line (``#`` .. ``######``) and runs up to
    (but not including) the next heading line -- so a heading and everything
    under it (list items, blockquotes, paragraphs) stays one candidate
    packing unit in ``chunk_text``. Content before the first heading, if any,
    is its own leading block with no heading. Text with no ATX headings at
    all returns ``[text]`` unchanged, so non-markdown/non-headed input is
    completely unaffected by this function's existence.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [text]

    blocks: list[str] = []
    if matches[0].start() > 0:
        blocks.append(text[: matches[0].start()])
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append(text[m.start() : end])
    return [b for b in blocks if b.strip()]


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Split *text* into pieces of at most *max_chars* characters.

    Prefers splitting at whitespace boundaries to avoid cutting mid-word when
    possible; falls back to hard character splits for content with no spaces.
    """
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        # Try to find the last space within the budget.
        window = remaining[:max_chars]
        cut = window.rfind(" ")
        if cut > 0:
            pieces.append(remaining[:cut])
            remaining = remaining[cut + 1:]
        else:
            # No space found — hard cut at exactly max_chars.
            pieces.append(remaining[:max_chars])
            remaining = remaining[max_chars:]
    if remaining.strip():
        pieces.append(remaining)
    return pieces
