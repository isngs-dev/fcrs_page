"""Unit tests for the analytics repository (S11.2).

Covers:
- Aggregation math over canned grouped message rows (intent/decision
  distribution, totals, fallback_rate, grounded_rate, deflection_rate).
- Zero-denominator -> None (MANDATORY no-silent-fallback), vs. a genuine
  0 numerator over a >0 denominator.
- Schedule conversion counts/rate.
- Series bucket-merge + sort.
- Bucket whitelist (INVALID_BUCKET) + parameterized (not interpolated) bucket.
- Global caller rejected (MANDATORY).
- Tenant scoping -- every query's $1 is claims.tenant_id (MANDATORY isolation).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.analytics.repository import get_analytics_overview

_WINDOW_FROM = datetime(2026, 7, 1, tzinfo=UTC)
_WINDOW_TO = datetime(2026, 7, 8, tzinfo=UTC)


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN, subject: str = "user-1") -> AuthClaims:
    return AuthClaims(subject=subject, role=role, tenant_id=tenant_id)


class _ScriptedDatabase:
    """Database double returning canned rows keyed by call order.

    ``get_analytics_overview`` issues, in order: message-facts fetch,
    conversation-totals fetchrow, schedule-conversion fetchrow, then three
    series fetches (messages, conversations, bookings).
    """

    def __init__(
        self,
        *,
        message_facts_rows: list[dict[str, Any]] | None = None,
        conversation_totals_row: dict[str, Any] | None = None,
        schedule_row: dict[str, Any] | None = None,
        series_message_rows: list[dict[str, Any]] | None = None,
        series_conversation_rows: list[dict[str, Any]] | None = None,
        series_booking_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._message_facts_rows = message_facts_rows or []
        self._conversation_totals_row = conversation_totals_row or {"total": 0, "escalated": 0}
        self._schedule_row = schedule_row or {"cta_total": 0, "converted": 0}
        self._series_message_rows = series_message_rows or []
        self._series_conversation_rows = series_conversation_rows or []
        self._series_booking_rows = series_booking_rows or []

        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        if "GROUP BY role, decision, grounded, intent" in query:
            return self._message_facts_rows
        if "FROM messages" in query and "date_trunc" in query:
            return self._series_message_rows
        if "FROM conversations" in query and "date_trunc" in query:
            return self._series_conversation_rows
        if "FROM schedule_events" in query and "date_trunc" in query:
            return self._series_booking_rows
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if "cta_convs" in query:
            return self._schedule_row
        if "FROM conversations c" in query:
            return self._conversation_totals_row
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        raise AssertionError("get_analytics_overview must not issue writes")

    async def close(self) -> None:
        pass


# -- Aggregation math ------------------------------------------------------


async def test_aggregation_math_from_canned_message_rows() -> None:
    """Grouped message rows -> correct intent/decision distributions + counts."""
    db = _ScriptedDatabase(
        message_facts_rows=[
            {"role": "user", "decision": None, "grounded": None, "intent": None, "cnt": 10},
            {"role": "bot", "decision": "answer", "grounded": True, "intent": "pricing", "cnt": 6},
            {"role": "bot", "decision": "answer", "grounded": False, "intent": "pricing", "cnt": 2},
            {"role": "bot", "decision": "escalate", "grounded": None, "intent": None, "cnt": 2},
            {"role": "bot", "decision": "clarify", "grounded": None, "intent": "support", "cnt": 1},
        ],
        conversation_totals_row={"total": 5, "escalated": 1},
    )
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.total_user_turns == 10
    assert overview.total_bot_turns == 11
    assert overview.decided_bot_turns == 11
    assert overview.intent_distribution == {"pricing": 8, "unclassified": 2, "support": 1}
    assert overview.decision_distribution == {"answer": 8, "escalate": 2, "clarify": 1}
    # fallback_rate = escalate(2) / decided(11)
    assert overview.fallback_rate == round(2 / 11, 4)
    # grounded_rate = grounded answers(6) / answers(8)
    assert overview.grounded_rate == round(6 / 8, 4)
    # deflection_rate = (total(5) - escalated(1)) / total(5)
    assert overview.deflection_rate == round(4 / 5, 4)


async def test_null_intent_bucketed_as_unclassified() -> None:
    """A NULL-intent bot row (S10.4 turn-cap escalate) -> 'unclassified' key."""
    db = _ScriptedDatabase(
        message_facts_rows=[
            {"role": "bot", "decision": "escalate", "grounded": None, "intent": None, "cnt": 3},
        ],
        conversation_totals_row={"total": 3, "escalated": 3},
    )
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.intent_distribution == {"unclassified": 3}


async def test_decision_distribution_excludes_null_decision_rows() -> None:
    """Bot rows with decision IS NULL never appear in decision_distribution."""
    db = _ScriptedDatabase(
        message_facts_rows=[
            {"role": "bot", "decision": None, "grounded": None, "intent": "chitchat", "cnt": 4},
            {"role": "bot", "decision": "answer", "grounded": True, "intent": "chitchat", "cnt": 1},
        ],
        conversation_totals_row={"total": 1, "escalated": 0},
    )
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.decision_distribution == {"answer": 1}
    # intent_distribution DOES include the undecided bot row.
    assert overview.intent_distribution == {"chitchat": 5}
    assert overview.decided_bot_turns == 1


# -- Zero-denominator -> None (MANDATORY) -----------------------------------


async def test_empty_window_all_rates_none_not_zero() -> None:
    """Empty window (all counts 0) -> all four rates are None, never 0.0."""
    db = _ScriptedDatabase()
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.fallback_rate is None
    assert overview.deflection_rate is None
    assert overview.grounded_rate is None
    assert overview.schedule_conversion_rate is None
    assert overview.total_conversations == 0
    assert overview.total_user_turns == 0
    assert overview.total_bot_turns == 0


async def test_traffic_with_no_escalations_gives_real_zero_and_one() -> None:
    """Traffic exists, zero escalations -> fallback_rate == 0.0, deflection_rate == 1.0."""
    db = _ScriptedDatabase(
        message_facts_rows=[
            {"role": "bot", "decision": "answer", "grounded": True, "intent": "pricing", "cnt": 5},
        ],
        conversation_totals_row={"total": 3, "escalated": 0},
    )
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.fallback_rate == 0.0
    assert overview.deflection_rate == 1.0


# -- Schedule conversion ------------------------------------------------------


async def test_schedule_conversion_counts_and_rate() -> None:
    db = _ScriptedDatabase(schedule_row={"cta_total": 4, "converted": 1})
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.schedule_cta_conversations == 4
    assert overview.schedule_conversions == 1
    assert overview.schedule_conversion_rate == round(1 / 4, 4)


async def test_schedule_conversion_zero_cta_conversations_gives_none_rate() -> None:
    db = _ScriptedDatabase(schedule_row={"cta_total": 0, "converted": 0})
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.schedule_cta_conversations == 0
    assert overview.schedule_conversion_rate is None


# -- Series --------------------------------------------------------------------


async def test_series_merges_per_bucket_rows_missing_metric_is_zero() -> None:
    """Per-bucket rows from the three queries merge by bucket; missing -> 0; sorted asc."""
    d1 = datetime(2026, 7, 1, tzinfo=UTC)
    d2 = datetime(2026, 7, 2, tzinfo=UTC)
    db = _ScriptedDatabase(
        series_message_rows=[
            {"bucket": d2, "answers": 3, "escalations": 1},
            {"bucket": d1, "answers": 1, "escalations": 0},
        ],
        series_conversation_rows=[
            {"bucket": d1, "conversations": 2},
        ],
        series_booking_rows=[
            {"bucket": d2, "bookings": 1},
        ],
    )
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert [b.bucket_start for b in overview.series] == [d1, d2]

    bucket_d1 = overview.series[0]
    assert bucket_d1.conversations == 2
    assert bucket_d1.answers == 1
    assert bucket_d1.escalations == 0
    assert bucket_d1.bookings == 0  # missing metric -> 0

    bucket_d2 = overview.series[1]
    assert bucket_d2.conversations == 0  # missing metric -> 0
    assert bucket_d2.answers == 3
    assert bucket_d2.escalations == 1
    assert bucket_d2.bookings == 1


# -- Bucket whitelist ------------------------------------------------------------


async def test_invalid_bucket_rejected_before_any_query() -> None:
    """bucket='hour' -> ValidationError INVALID_BUCKET, no query issued.

    SR-9.5 D3 widened `_VALID_BUCKETS` to include "month" (deliberate,
    disclosed contract widening -- see test_month_bucket_now_accepted
    below); "hour" remains rejected (explicitly out of scope, D3).
    """
    db = _ScriptedDatabase()
    claims = _claims("tenant-a")

    with pytest.raises(ValidationError) as exc_info:
        await get_analytics_overview(
            db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="hour",
        )

    assert exc_info.value.code == "INVALID_BUCKET"
    assert db.fetch_calls == []
    assert db.fetchrow_calls == []


async def test_month_bucket_now_accepted_sr95_d3() -> None:
    """SR-9.5 D3: `_VALID_BUCKETS` is deliberately widened to
    {"day", "week", "month"} -- a request that previously raised
    INVALID_BUCKET for "month" now succeeds. This is the explicit
    regression test for the widened contract."""
    db = _ScriptedDatabase()
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="month",
    )

    assert overview.bucket == "month"


async def test_week_bucket_bound_as_parameter_not_interpolated() -> None:
    """bucket='week' is bound as the $4 parameter to date_trunc, not string-interpolated."""
    db = _ScriptedDatabase()
    claims = _claims("tenant-a")

    await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="week",
    )

    series_calls = [call for call in db.fetch_calls if "date_trunc" in call[0]]
    assert series_calls, "expected series queries to run"
    for query, params in series_calls:
        assert "date_trunc($4::text" in query
        assert "week" not in query  # never string-interpolated into the SQL text
        assert params[3] == "week"


async def test_day_bucket_default() -> None:
    db = _ScriptedDatabase()
    claims = _claims("tenant-a")

    overview = await get_analytics_overview(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert overview.bucket == "day"


# -- Global caller rejected (MANDATORY) ------------------------------------------


async def test_platform_admin_rejected_no_query_issued() -> None:
    """PLATFORM_ADMIN (tenant_id=None) -> ValidationError, no query issued."""
    db = _ScriptedDatabase()
    claims = _claims(None, Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_analytics_overview(
            db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
        )

    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"
    assert db.fetch_calls == []
    assert db.fetchrow_calls == []


# -- Tenant scoping (MANDATORY isolation) ----------------------------------------


async def test_every_query_pins_tenant_id_as_first_param() -> None:
    """Tenant A vs tenant B claims -> every captured query's $1 is claims.tenant_id."""
    db_a = _ScriptedDatabase()
    claims_a = _claims("tenant-a")
    await get_analytics_overview(db_a, claims_a, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    for _, params in db_a.fetch_calls:
        assert params[0] == "tenant-a"
    for _, params in db_a.fetchrow_calls:
        assert params[0] == "tenant-a"

    db_b = _ScriptedDatabase()
    claims_b = _claims("tenant-b")
    await get_analytics_overview(db_b, claims_b, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    for _, params in db_b.fetch_calls:
        assert params[0] == "tenant-b"
    for _, params in db_b.fetchrow_calls:
        assert params[0] == "tenant-b"
