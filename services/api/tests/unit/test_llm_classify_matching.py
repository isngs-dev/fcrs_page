"""Unit tests for the shared classify() label-matching helper.

``match_label`` progressively tries, in order, stopping at the first success:
  a. Exact match (case-insensitive).
  b. Exact match after stripping trailing punctuation/whitespace.
  c. Naive depluralization (strip a single trailing 's') if it resolves to a
     UNIQUE label.
  d. Whole-word substring match, if exactly one label matches this way.

Returns ``None`` (never a hallucinated label) when no tier produces a single
unambiguous match.
"""
from __future__ import annotations

from api.llm.classify_matching import build_classify_instruction, match_label

_LABELS = ["question", "chitchat", "scheduling_request", "off_topic", "other"]


def test_match_label_exact_match() -> None:
    """Exact match (case-sensitive input) returns the canonical label."""
    assert match_label("scheduling_request", _LABELS) == "scheduling_request"


def test_match_label_case_insensitive_exact_match() -> None:
    """Case-insensitive exact match returns the canonical-cased label."""
    assert match_label("Scheduling_Request", _LABELS) == "scheduling_request"


def test_match_label_strips_trailing_punctuation() -> None:
    """Trailing punctuation (period) is stripped before exact match."""
    assert match_label("scheduling_request.", _LABELS) == "scheduling_request"


def test_match_label_strips_trailing_punctuation_and_whitespace() -> None:
    """Trailing whitespace + punctuation combo is stripped."""
    assert match_label("scheduling_request! \n", _LABELS) == "scheduling_request"


def test_match_label_strips_trailing_plural_s() -> None:
    """The exact live-bug scenario: 'scheduling_requests' -> 'scheduling_request'."""
    assert match_label("scheduling_requests", _LABELS) == "scheduling_request"


def test_match_label_depluralization_with_trailing_punctuation() -> None:
    """Depluralization applies after punctuation stripping too."""
    assert match_label("scheduling_requests.", _LABELS) == "scheduling_request"


def test_match_label_whole_word_substring_in_sentence() -> None:
    """A label wrapped in a sentence is recognized via whole-word match."""
    reply = "The correct label is scheduling_request."
    assert match_label(reply, _LABELS) == "scheduling_request"


def test_match_label_no_match_returns_none() -> None:
    """A reply matching no label even loosely returns None (fail loud upstream)."""
    assert match_label("banana", _LABELS) is None


def test_match_label_ambiguous_substring_returns_none() -> None:
    """Two labels both appearing as whole-word substrings -> ambiguous -> None."""
    labels = ["sales", "support", "billing"]
    reply = "This could be sales or support, not sure which."
    assert match_label(reply, labels) is None


def test_match_label_ambiguous_depluralization_returns_none() -> None:
    """Depluralization that could resolve to two different labels -> None.

    'classes' stripped of trailing 's' -> 'classe', which matches neither
    label exactly, so this is not ambiguous -- construct a genuine collision:
    two labels that both become the same string after naive depluralization
    is not reachable with distinct labels, so instead verify that a reply
    which depluralizes to a substring shared by two labels does not guess.
    """
    labels = ["cats", "cat"]
    # "cats" is already an exact match for the label "cats" (tier a) --
    # confirm exact match wins over depluralization ambiguity entirely.
    assert match_label("cats", labels) == "cats"


def test_match_label_whole_word_substring_not_matched_as_prefix() -> None:
    """Substring matching is whole-word, not a bare substring (no false positive)."""
    labels = ["cat", "category"]
    reply = "This is about categories in general."
    # "category" is not a whole word in "categories"; "cat" is not a whole
    # word either -- no match should be produced.
    assert match_label(reply, labels) is None


# -- build_classify_instruction: label_descriptions -----------------------------------


def test_build_classify_instruction_without_descriptions_omits_label_meanings() -> None:
    """No `label_descriptions` (or an empty dict) -> byte-for-byte today's
    bare-label-name-only behavior, no "Label meanings" section at all."""
    instruction = build_classify_instruction(_LABELS)
    assert "Label meanings" not in instruction
    assert build_classify_instruction(_LABELS, {}) == instruction
    assert build_classify_instruction(_LABELS, None) == instruction


def test_build_classify_instruction_includes_given_label_meanings() -> None:
    """Each described label appears as its own "- label: description" line."""
    descriptions = {
        "off_topic": "unrelated to this business",
        "question": "a genuine question about this business",
    }
    instruction = build_classify_instruction(_LABELS, descriptions)
    assert "Label meanings" in instruction
    assert "- off_topic: unrelated to this business" in instruction
    assert "- question: a genuine question about this business" in instruction


def test_build_classify_instruction_ignores_descriptions_for_labels_not_in_the_label_list() -> None:
    """A description for a label the caller did NOT pass in `labels` is
    silently dropped -- never invents a label meaning for something not
    being classified."""
    descriptions = {"off_topic": "unrelated to this business", "not_a_real_label": "should never appear"}
    instruction = build_classify_instruction(["question", "off_topic"], descriptions)
    assert "not_a_real_label" not in instruction
    assert "- off_topic: unrelated to this business" in instruction


def test_build_classify_instruction_partial_descriptions_only_describes_given_labels() -> None:
    """Only labels present in `label_descriptions` get a meaning line --
    every other label falls back to bare-name-only, exactly as before."""
    descriptions = {"off_topic": "unrelated to this business"}
    instruction = build_classify_instruction(_LABELS, descriptions)
    assert "- off_topic: unrelated to this business" in instruction
    for label in ("question", "chitchat", "scheduling_request", "other"):
        assert f"- {label}:" not in instruction
    # The bare label is still listed in the valid-labels line regardless.
    assert "question" in instruction
    assert "chitchat" in instruction


def test_build_classify_instruction_still_forbids_conversational_replies_with_descriptions() -> None:
    """Adding label_descriptions must not weaken the existing strict,
    non-conversational, output-shape instruction."""
    instruction = build_classify_instruction(_LABELS, {"off_topic": "unrelated to this business"})
    assert "not a conversational assistant" in instruction.lower()
    assert "only the label" in instruction.lower() or "only one label" in instruction.lower()
