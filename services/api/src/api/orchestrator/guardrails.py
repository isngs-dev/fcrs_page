"""Pure output guardrail scanner (S10.3).

``scan_output`` is a deterministic, closed-set, code-owned denylist scan run
on *generated* replies (the grounded-``answer`` and ``chitchat`` branches of
``answer_turn``) after ``provider.generate`` returns and before the reply is
stored/returned. It is intentionally NOT an LLM/ML classifier: no I/O, no
per-tenant state, no randomness -- a fixed, reviewable substring/regex denylist
so the behavior is exhaustively unit-testable (decision 3).

Four rules, checked in this LOCKED order -- the FIRST violated rule wins:

1. ``empty_output`` -- degenerate empty/whitespace-only generation.
2. ``instruction_leak`` -- the reply echoes one of our own system-prompt
   sentinels, or a known prompt-injection "reveal your instructions" marker
   (the D5 defense: visitor input must never override system instructions,
   and if a weak model complies with an injection anyway, we catch the leak
   on the way OUT).
3. ``human_impersonation`` -- the reply denies being a bot / claims to be a
   real person.
4. ``context_sentinel_leak`` -- the reply echoes the internal
   "no relevant knowledge was found" retrieval scaffold verbatim, instead of
   answering (or honestly, paraphrasing "I don't know") from it.

All substring matching (rules 2-4) is done on ``_normalize``d text
(``casefold()`` + collapsed whitespace) so case/spacing variants can't evade
the scan. Rule 1 checks the raw ``strip()`` of the original text -- a
normalized-but-nonempty string can't trip it, but we want the *raw* emptiness
check to run first regardless.

``_NO_CONTEXT_LINE`` is owned by ``api.orchestrator.service`` (the grounding
prompt is built there). To avoid a circular import (``service.py`` imports
``scan_output`` from this module), that constant is imported LAZILY inside
``scan_output`` itself -- by the time ``scan_output`` is actually called,
``service`` has finished importing, and the two modules always see the exact
same string (single source of truth -- never re-literaled here).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# -- Rule name constants (the only values ever written to messages.guardrail_flag) --
RULE_EMPTY_OUTPUT = "empty_output"
RULE_INSTRUCTION_LEAK = "instruction_leak"
RULE_HUMAN_IMPERSONATION = "human_impersonation"
RULE_CONTEXT_SENTINEL_LEAK = "context_sentinel_leak"

# -- Sentinel/denylist phrase sets (LOCKED, normalized/casefolded) -- decision 3 --
_INSTRUCTION_SENTINELS: tuple[str, ...] = (
    "answer the user's question using only the context",
    "do not answer business-specific questions",
    "invent facts, prices, or commitments",
    "you are a helpful assistant for this business",
    "reply briefly and warmly",
    "ignore previous instructions",
    "ignore all previous instructions",
    "system prompt",
)

_HUMAN_IMPERSONATION: tuple[str, ...] = (
    "i am a human",
    "i'm a human",
    "i am a real person",
    "i'm a real person",
    "i am not a bot",
    "i'm not a bot",
    "speaking to a real human",
)


@dataclass(frozen=True)
class GuardrailResult:
    """The outcome of ``scan_output`` -- ``ok=False`` carries the violated rule."""

    ok: bool
    rule: str | None


def _normalize(text: str) -> str:
    """``casefold()`` + collapse runs of whitespace to a single space."""
    return " ".join(text.casefold().split())


def scan_output(text: str) -> GuardrailResult:
    """Scan one generated reply against the 4 locked rules, in order.

    Pure function of ``text`` alone -- no I/O, no tenant/claims parameters,
    no LLM call, deterministic. Returns the FIRST violated rule, or
    ``GuardrailResult(ok=True, rule=None)`` when clean.
    """
    # Rule 1: empty_output -- checked on the raw strip(), before normalization.
    if not text.strip():
        return GuardrailResult(ok=False, rule=RULE_EMPTY_OUTPUT)

    normalized = _normalize(text)

    # Rule 2: instruction_leak.
    for sentinel in _INSTRUCTION_SENTINELS:
        if sentinel in normalized:
            return GuardrailResult(ok=False, rule=RULE_INSTRUCTION_LEAK)

    # Rule 3: human_impersonation.
    for phrase in _HUMAN_IMPERSONATION:
        if phrase in normalized:
            return GuardrailResult(ok=False, rule=RULE_HUMAN_IMPERSONATION)

    # Rule 4: context_sentinel_leak -- imported lazily to avoid a circular
    # import with api.orchestrator.service (which imports scan_output from
    # this module at module load time). Single source of truth: the string
    # is never re-literaled here.
    from api.orchestrator.service import _NO_CONTEXT_LINE  # noqa: PLC0415

    if _normalize(_NO_CONTEXT_LINE) in normalized:
        return GuardrailResult(ok=False, rule=RULE_CONTEXT_SENTINEL_LEAK)

    return GuardrailResult(ok=True, rule=None)


# -- Unstated commitments (A2 E7; shared with spoken call replies since A9 H4) ------------

_MONEY_OR_PERCENT = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s?%")
_COMMITMENT_WORDS = ("free", "discount", "guarantee", "guaranteed", "refund", "warranty",
                     "no obligation", "no-obligation", "risk-free")


def unstated_commitments(text: str, context: str) -> list[str]:
    """Amounts, percentages and commitment words in ``text`` that ``context`` doesn't contain."""
    ctx = context.casefold()
    found = [m.group(0) for m in _MONEY_OR_PERCENT.finditer(text)
             if re.sub(r"\s", "", m.group(0)) not in re.sub(r"\s", "", ctx)]
    lowered = text.casefold()
    found += [w for w in _COMMITMENT_WORDS
              if re.search(rf"\b{re.escape(w)}\b", lowered) and not re.search(
                  rf"\b{re.escape(w)}\b", ctx)]
    return found
