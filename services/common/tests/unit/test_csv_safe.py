"""Unit tests for common.csv_safe.escape_csv_cell (SR-9.5 D9).

Covers:
- Dangerous leading characters (=, +, -, @, TAB, CR) get a single-quote
  prefix.
- Safe values pass through byte-identical.
- Non-string values stringify first.
- None -> "".
- Legitimate data starting with '-' is preserved (prefixed, not stripped).
- End-to-end formula-injection example from the spec.
"""
from __future__ import annotations

from common.csv_safe import escape_csv_cell


def test_none_becomes_empty_string() -> None:
    assert escape_csv_cell(None) == ""


def test_safe_string_passes_through_unchanged() -> None:
    assert escape_csv_cell("hello world") == "hello world"
    assert escape_csv_cell("") == ""


def test_integer_stringifies() -> None:
    assert escape_csv_cell(42) == "42"


def test_float_stringifies() -> None:
    assert escape_csv_cell(3.14) == "3.14"


def test_decimal_stringifies() -> None:
    from decimal import Decimal

    assert escape_csv_cell(Decimal("19.99")) == "19.99"


def test_equals_leading_char_prefixed() -> None:
    assert escape_csv_cell("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"


def test_plus_leading_char_prefixed() -> None:
    assert escape_csv_cell("+1+1") == "'+1+1"


def test_minus_leading_char_prefixed() -> None:
    assert escape_csv_cell("-2+3+cmd|' /C calc'!A0") == "'-2+3+cmd|' /C calc'!A0"


def test_at_leading_char_prefixed() -> None:
    assert escape_csv_cell("@SUM(1+1)") == "'@SUM(1+1)"


def test_tab_leading_char_prefixed() -> None:
    assert escape_csv_cell("\t=1+1") == "'\t=1+1"


def test_cr_leading_char_prefixed() -> None:
    assert escape_csv_cell("\r=1+1") == "'\r=1+1"


def test_legitimate_value_starting_with_minus_is_preserved_not_stripped() -> None:
    """D9's data-preservation requirement: '-15% under budget' is real text.

    Prefixing (not stripping/rejecting) keeps the actual content intact --
    a spreadsheet reader sees "-15% under budget" as literal text, not a
    formula, and the underlying data is not destroyed.
    """
    result = escape_csv_cell("-15% under budget")
    assert result == "'-15% under budget"
    # The original text, minus the safety prefix, is fully recoverable.
    assert result[1:] == "-15% under budget"


def test_end_to_end_injection_example_from_spec() -> None:
    """The exact close_reason value named in the SR-9.5 DoD/tests section."""
    malicious = "=cmd|'/c calc'!A1"
    result = escape_csv_cell(malicious)
    assert result.startswith("'")
    assert not result.startswith("=")
    assert result == f"'{malicious}"
