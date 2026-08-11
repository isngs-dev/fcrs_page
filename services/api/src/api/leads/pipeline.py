"""Lead pipeline -- pure stage state machine + qualification scoring.

No I/O. Everything here is a pure function so it can be unit-tested without a
database and reused identically by the admin routes.

Stage state machine (S7.2 decision 1)
--------------------------------------
``STAGE_ORDER`` is the forward funnel. From a non-terminal stage ``S`` the
only legal transitions are:

- to the immediate next stage in ``STAGE_ORDER`` (forward one step), or
- to ``"disqualified"`` (an off-ramp available from any non-terminal stage).

Skipping a stage, moving backward, re-entering ``"captured"``, a no-op
(``X -> X``), or any transition *out of* a terminal stage
(``TERMINAL_STAGES``) is illegal and raises ``ValidationError``
(``INVALID_STAGE_TRANSITION``).

Status derivation (S7.2 decision 2)
------------------------------------
``status`` is never set directly by callers -- it is always derived from
``stage`` via ``status_for_stage``.

Qualification score (S7.2 decision 3)
--------------------------------------
``compute_qualification_score`` is a pure, deterministic function of a
``Lead``'s current fields, clamped to ``[0, 100]``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from common.errors import ValidationError

if TYPE_CHECKING:
    from api.leads.repository import Lead

STAGE_ORDER: list[str] = ["captured", "qualified", "contacted", "converted"]
"""The forward funnel, in order."""

TERMINAL_STAGES: set[str] = {"converted", "disqualified"}
"""Stages from which no further transition is permitted."""

_STATUS_BY_STAGE: dict[str, str] = {
    "captured": "new",
    "qualified": "open",
    "contacted": "open",
    "converted": "won",
    "disqualified": "lost",
}

# SR-25 D2: lead-list ordering is derived from the state-machine constants,
# never copied into SQL by hand. ``converted`` is already in STAGE_ORDER, so
# only the terminal off-ramp is appended.
_STAGES_IN_SORT_ORDER = [
    *STAGE_ORDER,
    *sorted(stage for stage in TERMINAL_STAGES if stage not in STAGE_ORDER),
]
STAGE_SORT_POSITION: dict[str, int] = {
    stage: position for position, stage in enumerate(_STAGES_IN_SORT_ORDER, start=1)
}
"""Canonical pipeline position used by lead-list stage sorting (SR-25 D2)."""

STATUS_SORT_POSITION: dict[str, int] = {
    status: position
    for position, status in enumerate(dict.fromkeys(_STATUS_BY_STAGE.values()), start=1)
}
"""Canonical lifecycle position used by lead-list status sorting (SR-25 D2)."""

# -- Qualification score weights (module constants; documented, tunable
#    later; not per-tenant this sprint) ---------------------------------
_SCORE_EMAIL_PRESENT = 30
_SCORE_PHONE_PRESENT = 25
_SCORE_NAME_PRESENT = 15

_SCORE_SOURCE_REFERRAL = 20
_SCORE_SOURCE_WIDGET = 10
_SCORE_SOURCE_OTHER = 0

_SCORE_STAGE_QUALIFIED = 10
_SCORE_STAGE_CONTACTED = 15
_SCORE_STAGE_CONVERTED = 20
_SCORE_STAGE_OTHER = 0  # captured, disqualified

_SCORE_MIN = 0
_SCORE_MAX = 100


def validate_transition(current: str, target: str) -> None:
    """Validate a proposed stage transition, raising on illegal moves.

    Raises ``ValidationError`` (code ``INVALID_STAGE_TRANSITION``) unless
    ``target`` is either the immediate next stage after ``current`` in
    ``STAGE_ORDER``, or ``"disqualified"`` from a non-terminal ``current``.
    """
    if current in TERMINAL_STAGES:
        _raise_invalid(current, target)

    if target == "disqualified":
        return

    if current not in STAGE_ORDER or target not in STAGE_ORDER:
        _raise_invalid(current, target)
        return

    current_index = STAGE_ORDER.index(current)
    target_index = STAGE_ORDER.index(target)
    if target_index != current_index + 1:
        _raise_invalid(current, target)


def _raise_invalid(current: str, target: str) -> None:
    raise ValidationError(
        f"Illegal stage transition from {current!r} to {target!r}.",
        code="INVALID_STAGE_TRANSITION",
    )


def status_for_stage(stage: str) -> str:
    """Return the derived ``status`` for a given ``stage``.

    Raises ``ValidationError`` for an unrecognized stage (defensive; callers
    should only ever pass a stage that already passed ``validate_transition``).
    """
    try:
        return _STATUS_BY_STAGE[stage]
    except KeyError:
        raise ValidationError(
            f"Unrecognized stage {stage!r}.",
            code="INVALID_STAGE_TRANSITION",
        ) from None


def compute_qualification_score(lead: Lead) -> int:
    """Compute a deterministic, side-effect-free qualification score, clamped [0, 100].

    Weights:
    - Contact completeness: email present +30, phone present +25, name present +15.
    - Source: ``referral`` +20, ``widget`` +10, else +0.
    - Stage progression: ``qualified`` +10, ``contacted`` +15, ``converted`` +20,
      ``captured``/``disqualified`` +0.
    """
    score = 0

    if lead.email:
        score += _SCORE_EMAIL_PRESENT
    if lead.phone:
        score += _SCORE_PHONE_PRESENT
    if lead.name:
        score += _SCORE_NAME_PRESENT

    if lead.source == "referral":
        score += _SCORE_SOURCE_REFERRAL
    elif lead.source == "widget":
        score += _SCORE_SOURCE_WIDGET
    else:
        score += _SCORE_SOURCE_OTHER

    if lead.stage == "qualified":
        score += _SCORE_STAGE_QUALIFIED
    elif lead.stage == "contacted":
        score += _SCORE_STAGE_CONTACTED
    elif lead.stage == "converted":
        score += _SCORE_STAGE_CONVERTED
    else:
        score += _SCORE_STAGE_OTHER

    return max(_SCORE_MIN, min(_SCORE_MAX, score))
