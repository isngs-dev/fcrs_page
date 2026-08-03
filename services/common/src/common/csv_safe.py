"""Formula-injection-safe CSV cell escaping (SR-9.5 D9).

A single, shared, pure helper -- ``escape_csv_cell`` -- used by every CSV
export in this codebase that writes a free-text or user-influenced cell.
Placed in ``services/common`` (not inside ``analytics/``) specifically so
other exports (e.g. ``leads/admin_routes.py``'s known-vulnerable
``_lead_to_csv_row``, SR-1.3's owned follow-up) can adopt it with a
two-line import.

The mitigation (standard OWASP CSV-injection guidance): a cell whose first
character is one of ``=``, ``+``, ``-``, ``@``, TAB, or CR is prefixed with
a single quote (``'``) so spreadsheet software (Excel, Google Sheets, etc.)
treats it as literal text instead of evaluating it as a formula. This is
applied to the RAW value BEFORE ``csv.writer`` performs its own quoting --
the two mechanisms are orthogonal: ``csv.writer``'s quoting protects the
*file format* (commas/quotes/newlines inside a field), this protects the
*consuming spreadsheet* (a cell that looks like a formula to Excel).

Deliberately preserves legitimate data rather than destroying it: a value
that merely *starts* with ``-`` (e.g. ``"-15% under budget"``) is prefixed,
never stripped or rejected.
"""
from __future__ import annotations

from typing import Any

_DANGEROUS_LEADING_CHARS = ("=", "+", "-", "@", "\t", "\r")


def escape_csv_cell(value: Any) -> str:
    """Return a formula-injection-safe string form of ``value``.

    - ``None`` becomes ``""``.
    - Non-string values are stringified first (``str(value)``).
    - If the resulting string's first character is one of ``=``, ``+``,
      ``-``, ``@``, TAB, or CR, the string is prefixed with a single quote
      (``'``) so a spreadsheet renders it as literal text rather than
      evaluating it as a formula.
    - Safe values (including an empty string) are returned unchanged.
    """
    if value is None:
        return ""

    text = value if isinstance(value, str) else str(value)

    if text.startswith(_DANGEROUS_LEADING_CHARS):
        return f"'{text}"

    return text
