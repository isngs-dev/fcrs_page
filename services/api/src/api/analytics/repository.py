"""Analytics repository -- tenant-scoped aggregation SQL over conversation
outcomes (S11.2).

Reads the *stored facts* S10.2 (``messages.intent``/``decision``/
``grounded``), S10.3 (``decision='blocked'`` + ``guardrail_flag``), S10.4
(``messages.action``), and S8.1 (``schedule_events``) already tag on every
turn -- no new instrumentation, no new columns, pure read-only aggregation.

This module owns its own SQL against the shared ``messages``/
``conversations``/``schedule_events`` tables -- it does **not** import
``api.conversation_store.repository`` or ``api.scheduling.repository``
functions (those are row-level, tenant+visitor CRUD helpers; analytics needs
tenant-wide ``GROUP BY`` aggregates, a genuinely different query shape).

Every method:
- Takes ``AuthClaims`` and calls ``_reject_global(claims)`` (PLATFORM_ADMIN
  is rejected -- analytics is inherently tenant-scoped).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …),
  never a hardcoded index or string-formatted value.
- Never returns or accepts ``tenant_id``/``visitor_id``/``conversation_id``/
  ``message_id`` in its public result -- only aggregate counts, rounded
  rates, and closed-set label keys.

Schedule-conversion approximation (decision 6): ``schedule_events`` carries
``visitor_id`` but no ``conversation_id`` -- so a booking cannot be joined to
the exact conversation it followed. The S10.4->S8.1 widget flow works
entirely inside one visitor session (same ``visitor_id`` = ``claims.subject``
stamps both ``conversations`` and ``schedule_events``), so ``visitor_id`` is
the correct, non-fabricated join key -- but a booking is attributed to a CTA
conversation whenever the visitor has *any* booked event, not strictly one
that happened after the CTA / inside the window (the schema has no
CTA-timestamp link). This is a disclosed best-effort, not a fabricated
number; exact per-conversation attribution needs a future
``schedule_events.conversation_id`` FK (Open question 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

_VALID_BUCKETS = {"day", "week", "month"}


@dataclass(frozen=True)
class AnalyticsBucket:
    """One time-bucketed slice of the series."""

    bucket_start: datetime
    conversations: int
    answers: int
    escalations: int
    bookings: int


@dataclass(frozen=True)
class AnalyticsOverview:
    """The full tenant-scoped conversation-analytics overview."""

    window_from: datetime
    window_to: datetime
    bucket: str
    total_conversations: int
    total_user_turns: int
    total_bot_turns: int
    decided_bot_turns: int
    intent_distribution: dict[str, int]
    decision_distribution: dict[str, int]
    fallback_rate: float | None
    deflection_rate: float | None
    grounded_rate: float | None
    schedule_cta_conversations: int
    schedule_conversions: int
    schedule_conversion_rate: float | None
    series: list[AnalyticsBucket] = field(default_factory=list)


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Analytics is always tenant-scoped; a global caller has no tenant_id and
    therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Analytics is tenant-scoped.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


def _round_rate(numerator: int, denominator: int) -> float | None:
    """Round ``numerator / denominator`` to 4dp; ``None`` if denominator is 0.

    No-silent-fallback (CLAUDE.md §3): a zero-denominator rate is honestly
    ``None`` (JSON ``null``), never a fabricated ``0.0``.
    """
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


async def get_analytics_overview(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
    bucket: str = "day",
) -> AnalyticsOverview:
    """Compute the tenant-scoped conversation-analytics overview.

    Raises ``ValidationError`` (``GLOBAL_CALLER_NOT_PERMITTED``) for a global
    caller, and ``ValidationError`` (``INVALID_BUCKET``) if ``bucket`` is not
    in ``{"day", "week"}``. The half-open window ``[window_from, window_to)``
    is applied consistently: conversation-scoped metrics filter on
    ``conversations.started_at``, message-scoped metrics filter on
    ``messages.created_at``.
    """
    _reject_global(claims)

    if bucket not in _VALID_BUCKETS:
        raise ValidationError(
            f"bucket must be one of {sorted(_VALID_BUCKETS)}.",
            code="INVALID_BUCKET",
        )

    tenant_id = claims.tenant_id

    message_facts = await _fetch_message_facts(db, tenant_id, window_from, window_to)
    conv_totals = await _fetch_conversation_totals(db, tenant_id, window_from, window_to)
    schedule = await _fetch_schedule_conversion(db, tenant_id, window_from, window_to)
    series = await _fetch_series(db, tenant_id, window_from, window_to, bucket)

    return AnalyticsOverview(
        window_from=window_from,
        window_to=window_to,
        bucket=bucket,
        total_conversations=conv_totals["total"],
        total_user_turns=message_facts["total_user_turns"],
        total_bot_turns=message_facts["total_bot_turns"],
        decided_bot_turns=message_facts["decided_bot_turns"],
        intent_distribution=message_facts["intent_distribution"],
        decision_distribution=message_facts["decision_distribution"],
        fallback_rate=_round_rate(
            message_facts["escalate_turns"], message_facts["decided_bot_turns"]
        ),
        deflection_rate=_round_rate(
            conv_totals["total"] - conv_totals["escalated"], conv_totals["total"]
        ),
        grounded_rate=_round_rate(
            message_facts["grounded_answer_turns"], message_facts["answer_turns"]
        ),
        schedule_cta_conversations=schedule["cta_total"],
        schedule_conversions=schedule["converted"],
        schedule_conversion_rate=_round_rate(schedule["converted"], schedule["cta_total"]),
        series=series,
    )


async def _fetch_message_facts(
    db: Database, tenant_id: Any, window_from: datetime, window_to: datetime,
) -> dict[str, Any]:
    """One grouped scan over ``messages`` -> aggregate role/decision/grounded/intent facts."""
    rows = await db.fetch(
        "SELECT role, decision, grounded, intent, count(*) AS cnt "
        "FROM messages "
        "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3 "
        "GROUP BY role, decision, grounded, intent",
        tenant_id,
        window_from,
        window_to,
    )

    total_user_turns = 0
    total_bot_turns = 0
    decided_bot_turns = 0
    escalate_turns = 0
    answer_turns = 0
    grounded_answer_turns = 0
    intent_distribution: dict[str, int] = {}
    decision_distribution: dict[str, int] = {}

    for row in rows:
        role = row["role"]
        decision = row["decision"]
        grounded = row["grounded"]
        intent = row["intent"]
        cnt = int(row["cnt"])

        if role == "user":
            total_user_turns += cnt
        elif role == "bot":
            total_bot_turns += cnt

            intent_key = intent if intent is not None else "unclassified"
            intent_distribution[intent_key] = intent_distribution.get(intent_key, 0) + cnt

            if decision is not None:
                decided_bot_turns += cnt
                decision_distribution[decision] = decision_distribution.get(decision, 0) + cnt

                if decision == "escalate":
                    escalate_turns += cnt
                elif decision == "answer":
                    answer_turns += cnt
                    if grounded is True:
                        grounded_answer_turns += cnt

    return {
        "total_user_turns": total_user_turns,
        "total_bot_turns": total_bot_turns,
        "decided_bot_turns": decided_bot_turns,
        "escalate_turns": escalate_turns,
        "answer_turns": answer_turns,
        "grounded_answer_turns": grounded_answer_turns,
        "intent_distribution": intent_distribution,
        "decision_distribution": decision_distribution,
    }


async def _fetch_conversation_totals(
    db: Database, tenant_id: Any, window_from: datetime, window_to: datetime,
) -> dict[str, int]:
    """Conversation totals + deflection input (total vs. escalated conversations)."""
    row = await db.fetchrow(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE EXISTS ( "
        "    SELECT 1 FROM messages m "
        "    WHERE m.tenant_id = c.tenant_id "
        "      AND m.conversation_id = c.conversation_id "
        "      AND m.decision = 'escalate')) AS escalated "
        "FROM conversations c "
        "WHERE c.tenant_id = $1 AND c.started_at >= $2 AND c.started_at < $3",
        tenant_id,
        window_from,
        window_to,
    )
    if row is None:
        return {"total": 0, "escalated": 0}
    return {"total": int(row["total"]), "escalated": int(row["escalated"])}


async def _fetch_schedule_conversion(
    db: Database, tenant_id: Any, window_from: datetime, window_to: datetime,
) -> dict[str, int]:
    """Schedule conversion: CTA-offered conversations -> booked (visitor_id-correlated)."""
    row = await db.fetchrow(
        "WITH cta_convs AS ( "
        "  SELECT DISTINCT c.conversation_id, c.visitor_id "
        "  FROM conversations c "
        "  WHERE c.tenant_id = $1 AND c.started_at >= $2 AND c.started_at < $3 "
        "    AND EXISTS (SELECT 1 FROM messages m "
        "                WHERE m.tenant_id = c.tenant_id "
        "                  AND m.conversation_id = c.conversation_id "
        "                  AND m.action = 'schedule_cta') "
        ") "
        "SELECT count(*) AS cta_total, "
        "       count(*) FILTER (WHERE cta.visitor_id IS NOT NULL AND EXISTS ( "
        "           SELECT 1 FROM schedule_events se "
        "           WHERE se.tenant_id = $1 AND se.visitor_id = cta.visitor_id "
        "             AND se.status = 'booked')) AS converted "
        "FROM cta_convs cta",
        tenant_id,
        window_from,
        window_to,
    )
    if row is None:
        return {"cta_total": 0, "converted": 0}
    return {"cta_total": int(row["cta_total"]), "converted": int(row["converted"])}


async def _fetch_series(
    db: Database,
    tenant_id: Any,
    window_from: datetime,
    window_to: datetime,
    bucket: str,
) -> list[AnalyticsBucket]:
    """Bucketed time series: merge answers/escalations, conversations, bookings by bucket_start."""
    message_rows = await db.fetch(
        "SELECT date_trunc($4::text, created_at) AS bucket, "
        "       count(*) FILTER (WHERE role = 'bot' AND decision = 'answer')   AS answers, "
        "       count(*) FILTER (WHERE role = 'bot' AND decision = 'escalate') AS escalations "
        "FROM messages "
        "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3 "
        "GROUP BY 1",
        tenant_id,
        window_from,
        window_to,
        bucket,
    )
    conversation_rows = await db.fetch(
        "SELECT date_trunc($4::text, started_at) AS bucket, count(*) AS conversations "
        "FROM conversations "
        "WHERE tenant_id = $1 AND started_at >= $2 AND started_at < $3 "
        "GROUP BY 1",
        tenant_id,
        window_from,
        window_to,
        bucket,
    )
    booking_rows = await db.fetch(
        "SELECT date_trunc($4::text, created_at) AS bucket, count(*) AS bookings "
        "FROM schedule_events "
        "WHERE tenant_id = $1 AND status = 'booked' AND created_at >= $2 AND created_at < $3 "
        "GROUP BY 1",
        tenant_id,
        window_from,
        window_to,
        bucket,
    )

    merged: dict[datetime, dict[str, int]] = {}

    def _slot(bucket_start: datetime) -> dict[str, int]:
        return merged.setdefault(
            bucket_start,
            {"conversations": 0, "answers": 0, "escalations": 0, "bookings": 0},
        )

    for row in message_rows:
        slot = _slot(row["bucket"])
        slot["answers"] = int(row["answers"])
        slot["escalations"] = int(row["escalations"])

    for row in conversation_rows:
        slot = _slot(row["bucket"])
        slot["conversations"] = int(row["conversations"])

    for row in booking_rows:
        slot = _slot(row["bucket"])
        slot["bookings"] = int(row["bookings"])

    return [
        AnalyticsBucket(
            bucket_start=bucket_start,
            conversations=values["conversations"],
            answers=values["answers"],
            escalations=values["escalations"],
            bookings=values["bookings"],
        )
        for bucket_start, values in sorted(merged.items(), key=lambda item: item[0])
    ]
