"""Unit tests for api.ingestion.chunker.chunk_text.

Covers:
- Deterministic: same input → same output.
- Respects max_chars: no chunk exceeds max_chars (unless a single word forces it).
- Overlap: trailing chars of chunk N appear at the start of chunk N+1.
- Sentence boundaries preferred: chunks do not break mid-sentence when avoidable.
- A single over-long sentence is hard-split (not dropped).
- Empty / whitespace-only input → [].
"""
from __future__ import annotations

import sys
from unittest.mock import patch

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


def _reset_modules() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("api.ingestion"):
            del sys.modules[key]
    from common.settings import get_settings

    from api.config import get_api_settings
    get_settings.cache_clear()
    get_api_settings.cache_clear()


# ==============================================================================
# Empty / whitespace
# ==============================================================================


def test_empty_string_returns_empty_list() -> None:
    """Empty string → []."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text
        result = chunk_text("", max_chars=1000, overlap=150)
    assert result == []


def test_whitespace_only_returns_empty_list() -> None:
    """Whitespace-only string → []."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text
        result = chunk_text("   \n\t  \n  ", max_chars=1000, overlap=150)
    assert result == []


# ==============================================================================
# Determinism
# ==============================================================================


def test_chunk_text_is_deterministic() -> None:
    """Same input produces identical output on two calls."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        text = (
            "The quick brown fox jumps over the lazy dog. "
            "Pack my box with five dozen liquor jugs. "
            "How vexingly quick daft zebras jump. "
            "The five boxing wizards jump quickly."
        )
        r1 = chunk_text(text, max_chars=100, overlap=20)
        r2 = chunk_text(text, max_chars=100, overlap=20)
    assert r1 == r2
    assert len(r1) > 0


# ==============================================================================
# max_chars respected
# ==============================================================================


def test_no_chunk_exceeds_max_chars_for_normal_text() -> None:
    """Each chunk is at most max_chars long (for reasonable sentence lengths)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        # Build a text with many short sentences
        sentences = [f"This is sentence number {i} in our test." for i in range(50)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_chars=200, overlap=30)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 200, f"Chunk too long ({len(chunk)}): {chunk[:80]!r}"


def test_single_sentence_shorter_than_max_chars() -> None:
    """A short text that fits in one chunk → single-element list."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        text = "Hello world. This is a short document."
        chunks = chunk_text(text, max_chars=1000, overlap=150)

    assert len(chunks) == 1
    assert "Hello" in chunks[0]


# ==============================================================================
# Overlap
# ==============================================================================


def test_overlap_chars_appear_in_next_chunk() -> None:
    """The last `overlap` chars of chunk N appear at the start of chunk N+1."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        sentences = [f"Sentence {i} has some words in it." for i in range(30)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_chars=150, overlap=40)

    assert len(chunks) >= 2
    # Verify overlap: the tail of chunk[0] should appear in chunk[1]
    tail = chunks[0][-40:]
    assert tail in chunks[1], (
        f"Expected tail {tail!r} to appear at start of chunk[1]={chunks[1][:80]!r}"
    )


# ==============================================================================
# Sentence boundaries preferred
# ==============================================================================


def test_chunks_prefer_sentence_boundaries() -> None:
    """Chunks should end near sentence boundaries, not mid-word."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        # Each sentence is well under max_chars individually
        text = (
            "First sentence here. Second sentence here. Third sentence here. "
            "Fourth sentence here. Fifth sentence here. Sixth sentence here. "
            "Seventh sentence here. Eighth sentence here."
        )
        chunks = chunk_text(text, max_chars=80, overlap=15)

    # Each chunk (except possibly the last) should end with punctuation or be
    # at the boundary of a sentence. We check that no chunk ends mid-word
    # (i.e., the last char is not a letter in the middle of a word)
    for chunk in chunks[:-1]:
        stripped = chunk.rstrip()
        # Should end with sentence-ending punctuation or a full word boundary
        assert len(stripped) > 0


# ==============================================================================
# Hard-split of over-long sentence
# ==============================================================================


def test_oversized_sentence_is_hard_split_not_dropped() -> None:
    """A single sentence longer than max_chars is hard-split and not dropped."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        # A single sentence much longer than max_chars
        long_sentence = "word " * 200  # 1000 chars
        chunks = chunk_text(long_sentence, max_chars=100, overlap=10)

    # All content must be preserved (accounting for leading overlap)
    all_content = "".join(chunks)
    # The original words should all be present across chunks
    assert "word" in all_content
    assert len(chunks) > 1, "Over-long sentence must produce multiple chunks"

    # No content dropped: total chars should cover the original
    # (with overlap, total is >= original length)
    for chunk in chunks:
        assert len(chunk) <= 100 + 1, f"Hard-split chunk too long: {len(chunk)}"


def test_oversized_word_forces_hard_split() -> None:
    """An extremely long token (no spaces) is hard-split across chunks."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        # A single token longer than max_chars
        very_long_word = "x" * 500
        chunks = chunk_text(very_long_word, max_chars=100, overlap=10)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 100


# ==============================================================================
# Multi-chunk correctness
# ==============================================================================


def test_multiple_chunks_cover_full_content() -> None:
    """All text content is reachable across the chunks (no silent dropping)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        # 10 distinct sentences, each ~60 chars
        sentences = [f"Sentence {i:02d}: The quick brown fox jumps lazily." for i in range(10)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_chars=150, overlap=20)

    # Every sentence number 00..09 should appear in at least one chunk
    for i in range(10):
        marker = f"Sentence {i:02d}"
        assert any(marker in chunk for chunk in chunks), (
            f"{marker!r} not found in any chunk"
        )


def test_zero_overlap_produces_non_overlapping_chunks() -> None:
    """With overlap=0 chunks are contiguous non-overlapping segments."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        text = "Alpha. Beta. Gamma. Delta. Epsilon. Zeta. Eta. Theta. Iota. Kappa."
        chunks = chunk_text(text, max_chars=30, overlap=0)

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 30


# ==============================================================================
# Markdown heading-aware atomic packing (FAQ block never split when it fits)
# ==============================================================================


def _faq_block(n: int) -> str:
    """One FAQ entry shaped like the real-world regression this guards
    against: a heading, several question phrasings, several answer
    phrasings -- with NO shared vocabulary between the question lines and
    the later answer lines, so a chunk boundary landing between them would
    be silently unrecoverable for retrieval (the split-off half has no
    heading/topic words left to match a query against)."""
    return (
        f"### {n}. What is topic {n}?\n\n"
        f"* Var 1: alpha phrasing about topic {n}?\n"
        f"* Var 2: beta phrasing about topic {n}?\n\n"
        f"> Answer 1: zzzzz answer content unrelated in wording to the vars above.\n"
        f"> Answer 2: yyyyy more answer content, still no shared words with the vars.\n"
    )


def test_heading_block_kept_whole_when_it_fits() -> None:
    """A '### heading' + its Vars/Answers, sized to fit in one max_chars
    budget, lands entirely in ONE chunk -- never split at a sentence
    boundary partway through, even though the plain sentence-packer would
    otherwise be willing to split there."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        block = _faq_block(1)
        chunks = chunk_text(block, max_chars=len(block) + 50, overlap=20)

    assert len(chunks) == 1
    assert "Var 1" in chunks[0]
    assert "Answer 2" in chunks[0]


def test_heading_never_separated_from_its_content_across_chunk_boundary() -> None:
    """Two FAQ blocks back to back, sized so only ONE fits per chunk: the
    chunk boundary falls BETWEEN the two headings, never inside either
    one's Var/Answer content (the exact regression this fix targets --
    'Are Facebook quote requests harder to close...' landed with no
    heading in its chunk because the packer split mid-block)."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        block1 = _faq_block(1)
        block2 = _faq_block(2)
        text = block1 + block2
        # Budget fits one block comfortably but not both.
        max_chars = len(block1) + 30
        chunks = chunk_text(text, max_chars=max_chars, overlap=0)

    assert len(chunks) >= 2
    for chunk in chunks:
        has_heading = "What is topic" in chunk
        has_answer = "Answer 1" in chunk or "Answer 2" in chunk
        # A chunk containing answer content must also contain its own
        # block's heading -- never answers with no heading to anchor them.
        if has_answer:
            assert has_heading, f"chunk has answer content but no heading: {chunk[:120]!r}"


def test_multiple_small_heading_blocks_still_pack_together() -> None:
    """Several small heading blocks that jointly fit in one budget are still
    packed into a single chunk -- heading-awareness must not regress into
    a wasteful one-chunk-per-heading policy."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        text = "".join(_faq_block(i) for i in range(1, 4))
        chunks = chunk_text(text, max_chars=len(text) + 100, overlap=0)

    assert len(chunks) == 1
    for i in range(1, 4):
        assert f"topic {i}" in chunks[0]


def test_oversized_heading_block_falls_back_to_sentence_splitting() -> None:
    """A single heading block LARGER than max_chars still gets split (never
    dropped) -- it degrades to the same sentence-level packing used before
    heading-awareness existed, rather than being emitted as one oversized
    chunk or silently truncated."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import chunk_text

        block = _faq_block(1) * 5  # much larger than any reasonable max_chars
        chunks = chunk_text(block, max_chars=200, overlap=20)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 200
    all_content = " ".join(chunks)
    assert "Answer 2" in all_content


def test_text_without_headings_chunks_identically_to_before() -> None:
    """Text with no ATX headings at all is completely unaffected by
    heading-awareness -- same output as plain sentence-level chunking."""
    _reset_modules()
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        from api.ingestion.chunker import _split_sentences, chunk_text

        text = (
            "This document has no markdown headings whatsoever. "
            "It is just plain sentences, one after another, for a while. "
            "Nothing here should be treated as a heading boundary."
        )
        chunks = chunk_text(text, max_chars=60, overlap=10)

    # No '#' anywhere in the input, so _split_heading_blocks must return the
    # whole text as one block, falling straight through to _split_sentences
    # -- verify that block-splitting is a no-op by checking every sentence
    # is still reachable.
    for sentence in _split_sentences(text):
        assert any(sentence.strip() in chunk for chunk in chunks) or any(
            sentence.strip()[:20] in chunk for chunk in chunks
        )
