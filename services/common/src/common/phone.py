"""Phone-number normalisation shared across modules (calls, contact identities,
outreach compliance). Pure; no I/O.

The platform's market is the US (widget phone fields are US-formatted), so a
bare 10-digit number -- or 11 digits starting with 1 -- is read as a US/Canada
number. Anything else must already carry its ``+``country code.
"""
from __future__ import annotations

import re


def to_e164(phone: str) -> str | None:
    """Return ``phone`` in E.164 form (``+<digits>``), or ``None`` when it can't be dialled."""
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None
