"""common.phone.to_e164 -- the one phone normaliser shared by calls, contact
identities, CSV imports and the outreach compliance gate (A1 D7)."""
from __future__ import annotations

import pytest

from common.phone import to_e164


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(786) 823-3553", "+17868233553"),   # US 10-digit, widget format
        ("786.823.3553", "+17868233553"),
        ("1 786 823 3553", "+17868233553"),   # 11 digits with leading US 1
        ("+1 (786) 823-3553", "+17868233553"),
        ("+44 20 7946 0958", "+442079460958"),  # explicit country code kept
        ("  +17868233553  ", "+17868233553"),
    ],
)
def test_normalises_dialable_numbers(raw: str, expected: str) -> None:
    assert to_e164(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "12345", "823-3553", "+123", "2 786 823 3553"])
def test_returns_none_for_numbers_that_cannot_be_dialled(raw: str) -> None:
    assert to_e164(raw) is None
