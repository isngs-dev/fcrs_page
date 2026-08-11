"""Unit tests for api.analytics.reports_repository (SR-9.5).

Covers all four reports:
- leads_by_stage: GROUP BY stage, every STAGE_ORDER + disqualified key
  present with explicit 0, includes converted/tombstoned leads (D6/M7 --
  the highest-value correctness test in this sprint).
- bookings_series: buckets by created_at, cancelled visible + excluded from
  total_excluding_cancelled, completed/no_show counted (D5).
- conversion_funnel: leads-only, current-stage snapshot, includes converted
  leads, disqualified reported separately, drop_off_rate via _round_rate.
- win_loss: closed_at window, Decimal sums, NULL amounts excluded (never
  zeroed), avg_days_to_close, win_rate, ungrouped reverse-chronological
  loss_reasons, currency + currency_configured, no win_probability field
  anywhere (dataclass simply never has the field).

Plus MANDATORY: tenant isolation exact-value assertions, _reject_global for
every function, half-open boundary correctness, zero-denominator -> None
rates (never 0.0), Decimal exactness (19.99 + 0.01).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.analytics.reports_repository import (
    get_bookings_series,
    get_conversion_funnel,
    get_leads_by_stage,
    get_win_loss,
)

_WINDOW_FROM = datetime(2026, 7, 1, tzinfo=UTC)
_WINDOW_TO = datetime(2026, 8, 1, tzinfo=UTC)


def _claims(tenant_id: str | None, role: Role = Role.CLIENT_ADMIN, subject: str = "user-1") -> AuthClaims:
    return AuthClaims(subject=subject, role=role, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Shared stub DB -- an in-memory table store filtered by simple predicate
# matching against the actual SQL text, so tests exercise the real query
# shape (tenant filter, half-open window, extra filters) without a live DB.
# ---------------------------------------------------------------------------


class _StubDatabase:
    """Minimal in-memory stand-in for asyncpg-backed ``Database``.

    Holds three "tables" as lists of dicts (leads, schedule_events,
    opportunities) plus an optional tenant_opportunity_configs row set.
    Query dispatch is by substring match on ``FROM <table>`` -- enough to
    exercise the real repository code paths (WHERE tenant_id=$1, half-open
    window, GROUP BY, ORDER BY, LIMIT) against Python-side filtering.
    """

    def __init__(
        self,
        *,
        leads: list[dict[str, Any]] | None = None,
        schedule_events: list[dict[str, Any]] | None = None,
        opportunities: list[dict[str, Any]] | None = None,
        opportunity_configs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.leads = leads or []
        self.schedule_events = schedule_events or []
        self.opportunities = opportunities or []
        self.opportunity_configs = opportunity_configs or {}
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        raise AssertionError("Reports repository must not issue writes")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        q = query.upper()
        if "TENANT_OPPORTUNITY_CONFIGS" in q:
            tenant_id = args[0]
            row = self.opportunity_configs.get(tenant_id)
            if row is None:
                return None
            return {"currency": row["currency"], "stage_probabilities": row.get("stage_probabilities", {})}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        q = query.upper()
        if "FROM LEADS" in q:
            return self._query_leads(query, args)
        if "FROM SCHEDULE_EVENTS" in q:
            return self._query_bookings(query, args)
        if "FROM OPPORTUNITIES" in q:
            return self._query_opportunities(query, args)
        raise AssertionError(f"Unexpected fetch query: {query}")

    # -- leads ---------------------------------------------------------

    def _query_leads(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        tenant_id, window_from, window_to = args[0], args[1], args[2]
        idx = 3
        rows = [
            r for r in self.leads
            if r["tenant_id"] == tenant_id and window_from <= r["created_at"] < window_to
        ]
        q = query.upper()
        if " AND SOURCE = $" in q:
            source_val = args[idx]
            rows = [r for r in rows if r["source"] == source_val]
            idx += 1
        if " AND ASSIGNED_AGENT_ID = $" in q:
            agent_val = args[idx]
            rows = [r for r in rows if r["assigned_agent_id"] == agent_val]
            idx += 1

        grouped: dict[str, int] = {}
        for r in rows:
            grouped[r["stage"]] = grouped.get(r["stage"], 0) + 1
        return [{"stage": k, "cnt": v} for k, v in grouped.items()]

    # -- schedule_events -------------------------------------------------

    def _query_bookings(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        tenant_id, window_from, window_to = args[0], args[1], args[2]
        idx = 3
        rows = [
            r for r in self.schedule_events
            if r["tenant_id"] == tenant_id and window_from <= r["created_at"] < window_to
        ]
        q = query.upper()
        if " AND SOURCE = $" in q:
            source_val = args[idx]
            rows = [r for r in rows if r["source"] == source_val]
            idx += 1
        if " AND STATUS = $" in q:
            status_val = args[idx]
            rows = [r for r in rows if r["status"] == status_val]
            idx += 1
        bucket = args[idx]

        buckets: dict[datetime, dict[str, int]] = {}

        def _trunc(dt: datetime) -> datetime:
            if bucket == "day":
                return dt.replace(hour=0, minute=0, second=0, microsecond=0)
            if bucket == "week":
                start = dt - __import__("datetime").timedelta(days=dt.weekday())
                return start.replace(hour=0, minute=0, second=0, microsecond=0)
            if bucket == "month":
                return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            raise AssertionError(f"Unexpected bucket: {bucket}")

        for r in rows:
            b = _trunc(r["created_at"])
            slot = buckets.setdefault(b, {"booked": 0, "completed": 0, "no_show": 0, "cancelled": 0})
            slot[r["status"]] = slot.get(r["status"], 0) + 1

        return [
            {"bucket": b, "booked": v["booked"], "completed": v["completed"],
             "no_show": v["no_show"], "cancelled": v["cancelled"]}
            for b, v in sorted(buckets.items())
        ]

    # -- opportunities -----------------------------------------------------

    def _query_opportunities(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        q = query.upper()
        tenant_id, window_from, window_to = args[0], args[1], args[2]
        idx = 3
        rows = [
            r for r in self.opportunities
            if r["tenant_id"] == tenant_id
            and r["stage"] in ("closed_won", "closed_lost")
            and r["closed_at"] is not None
            and window_from <= r["closed_at"] < window_to
        ]
        if " AND OWNER_AGENT_ID = $" in q:
            owner_val = args[idx]
            rows = [r for r in rows if r["owner_agent_id"] == owner_val]
            idx += 1

        if "GROUP BY STAGE" in q:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                grouped.setdefault(r["stage"], []).append(r)
            out = []
            for stage, items in grouped.items():
                amounts = [i["amount"] for i in items if i["amount"] is not None]
                amount_total = sum(amounts) if amounts else None
                null_count = sum(1 for i in items if i["amount"] is None)
                days = [
                    (i["closed_at"] - i["created_at"]).total_seconds() / 86400.0
                    for i in items
                ]
                avg_days = sum(days) / len(days) if days else None
                out.append({
                    "stage": stage,
                    "cnt": len(items),
                    "amount_null_count": null_count,
                    "amount_total": amount_total,
                    "avg_days_to_close": avg_days,
                })
            return out

        if "STAGE = 'CLOSED_LOST'" in q and "ORDER BY CLOSED_AT DESC" in q:
            lost_rows = [r for r in rows if r["stage"] == "closed_lost"]
            lost_rows.sort(key=lambda r: r["closed_at"], reverse=True)
            limit = args[-1]
            lost_rows = lost_rows[:limit]
            return [{"closed_at": r["closed_at"], "close_reason": r["close_reason"]} for r in lost_rows]

        raise AssertionError(f"Unexpected opportunities query shape: {query}")


# ---------------------------------------------------------------------------
# leads_by_stage
# ---------------------------------------------------------------------------


def _lead(tenant_id: str, stage: str, created_at: datetime, **kwargs: Any) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "stage": stage,
        "created_at": created_at,
        "source": kwargs.get("source", "widget"),
        "assigned_agent_id": kwargs.get("assigned_agent_id"),
    }


async def test_leads_by_stage_every_stage_key_present_with_explicit_zero() -> None:
    db = _StubDatabase(leads=[])
    claims = _claims("tenant-a")

    result = await get_leads_by_stage(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.stages == {
        "captured": 0, "qualified": 0, "contacted": 0, "converted": 0, "disqualified": 0,
    }
    assert result.total == 0


async def test_leads_by_stage_includes_converted_tombstoned_leads() -> None:
    """D6/M7 -- THE trap: a naive query using list_leads' default would
    report converted=0. This must NOT happen."""
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 5, tzinfo=UTC)),
        _lead("tenant-a", "converted", datetime(2026, 7, 10, tzinfo=UTC)),
        _lead("tenant-a", "converted", datetime(2026, 7, 15, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_leads_by_stage(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.stages["converted"] == 2
    assert result.total == 3


async def test_leads_by_stage_exact_counts_per_stage() -> None:
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "captured", datetime(2026, 7, 3, tzinfo=UTC)),
        _lead("tenant-a", "qualified", datetime(2026, 7, 4, tzinfo=UTC)),
        _lead("tenant-a", "disqualified", datetime(2026, 7, 5, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_leads_by_stage(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.stages == {
        "captured": 2, "qualified": 1, "contacted": 0, "converted": 0, "disqualified": 1,
    }
    assert result.total == 4


async def test_leads_by_stage_source_filter_never_widens_scope() -> None:
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC), source="widget"),
        _lead("tenant-a", "captured", datetime(2026, 7, 3, tzinfo=UTC), source="referral"),
    ])
    claims = _claims("tenant-a")

    result = await get_leads_by_stage(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, source="referral",
    )

    assert result.stages["captured"] == 1
    assert result.total == 1


async def test_leads_by_stage_assigned_agent_filter() -> None:
    db = _StubDatabase(leads=[
        _lead("tenant-a", "qualified", datetime(2026, 7, 2, tzinfo=UTC), assigned_agent_id="agent-1"),
        _lead("tenant-a", "qualified", datetime(2026, 7, 3, tzinfo=UTC), assigned_agent_id="agent-2"),
    ])
    claims = _claims("tenant-a")

    result = await get_leads_by_stage(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, assigned_agent_id="agent-1",
    )

    assert result.stages["qualified"] == 1


async def test_leads_by_stage_half_open_window_boundary() -> None:
    """A row stamped exactly at from is included; exactly at to is excluded."""
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", _WINDOW_FROM),  # included
        _lead("tenant-a", "captured", _WINDOW_TO),    # excluded
    ])
    claims = _claims("tenant-a")

    result = await get_leads_by_stage(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.total == 1
    assert result.stages["captured"] == 1


async def test_leads_by_stage_rejects_global_caller() -> None:
    db = _StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_leads_by_stage(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_leads_by_stage_tenant_isolation_exact_values() -> None:
    """MANDATORY: tenant A's report equals A's exact numbers, unaffected by B."""
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "qualified", datetime(2026, 7, 3, tzinfo=UTC)),
        _lead("tenant-b", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-b", "captured", datetime(2026, 7, 3, tzinfo=UTC)),
        _lead("tenant-b", "captured", datetime(2026, 7, 4, tzinfo=UTC)),
        _lead("tenant-b", "converted", datetime(2026, 7, 5, tzinfo=UTC)),
    ])

    result_a = await get_leads_by_stage(
        db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )
    result_b = await get_leads_by_stage(
        db, _claims("tenant-b"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    assert result_a.stages == {
        "captured": 1, "qualified": 1, "contacted": 0, "converted": 0, "disqualified": 0,
    }
    assert result_a.total == 2
    assert result_b.stages == {
        "captured": 3, "qualified": 0, "contacted": 0, "converted": 1, "disqualified": 0,
    }
    assert result_b.total == 4


# ---------------------------------------------------------------------------
# bookings_series
# ---------------------------------------------------------------------------


def _booking(tenant_id: str, status: str, created_at: datetime, **kwargs: Any) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "status": status,
        "created_at": created_at,
        "source": kwargs.get("source", "native"),
    }


async def test_bookings_series_cancelled_visible_and_excluded_from_total() -> None:
    """D5 -- cancelled is its own series value, excluded from
    total_excluding_cancelled; completed/no_show are counted, never dropped."""
    db = _StubDatabase(schedule_events=[
        _booking("tenant-a", "booked", datetime(2026, 7, 6, tzinfo=UTC)),
        _booking("tenant-a", "cancelled", datetime(2026, 7, 6, tzinfo=UTC)),
        _booking("tenant-a", "completed", datetime(2026, 7, 6, tzinfo=UTC)),
        _booking("tenant-a", "no_show", datetime(2026, 7, 6, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_bookings_series(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="week",
    )

    assert result.totals.cancelled == 1
    assert result.totals.completed == 1
    assert result.totals.no_show == 1
    assert result.totals.booked == 1
    # total_excluding_cancelled = booked + completed + no_show = 3 (cancelled excluded)
    assert result.totals.total_excluding_cancelled == 3


async def test_bookings_series_buckets_by_created_at_not_starts_at() -> None:
    """D5 -- date basis is created_at. A booking created inside the window
    is counted regardless of starts_at (this repo doesn't even read
    starts_at)."""
    db = _StubDatabase(schedule_events=[
        _booking("tenant-a", "booked", datetime(2026, 7, 6, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_bookings_series(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="day",
    )

    assert result.totals.booked == 1


async def test_bookings_series_buckets_tile_without_gap_or_overlap() -> None:
    db = _StubDatabase(schedule_events=[
        _booking("tenant-a", "booked", datetime(2026, 7, 3, tzinfo=UTC)),
        _booking("tenant-a", "booked", datetime(2026, 7, 10, tzinfo=UTC)),
        _booking("tenant-a", "booked", datetime(2026, 7, 17, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_bookings_series(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="week",
    )

    bucket_sum = sum(b.booked for b in result.series)
    assert bucket_sum == result.totals.booked == 3


async def test_bookings_series_source_and_status_filters() -> None:
    db = _StubDatabase(schedule_events=[
        _booking("tenant-a", "booked", datetime(2026, 7, 6, tzinfo=UTC), source="native"),
        _booking("tenant-a", "booked", datetime(2026, 7, 6, tzinfo=UTC), source="calendly"),
    ])
    claims = _claims("tenant-a")

    result = await get_bookings_series(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="week", source="calendly",
    )

    assert result.totals.booked == 1


async def test_bookings_series_month_bucket_succeeds() -> None:
    """D3 -- month bucket must work on the new report too."""
    db = _StubDatabase(schedule_events=[
        _booking("tenant-a", "booked", datetime(2026, 7, 6, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_bookings_series(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="month",
    )

    assert result.totals.booked == 1


async def test_bookings_series_rejects_global_caller() -> None:
    db = _StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_bookings_series(
            db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="week",
        )
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_bookings_series_tenant_isolation_exact_values() -> None:
    db = _StubDatabase(schedule_events=[
        _booking("tenant-a", "booked", datetime(2026, 7, 6, tzinfo=UTC)),
        _booking("tenant-b", "booked", datetime(2026, 7, 6, tzinfo=UTC)),
        _booking("tenant-b", "booked", datetime(2026, 7, 7, tzinfo=UTC)),
        _booking("tenant-b", "cancelled", datetime(2026, 7, 8, tzinfo=UTC)),
    ])

    result_a = await get_bookings_series(
        db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="week",
    )
    result_b = await get_bookings_series(
        db, _claims("tenant-b"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO, bucket="week",
    )

    assert result_a.totals.booked == 1
    assert result_b.totals.booked == 2
    assert result_b.totals.cancelled == 1


# ---------------------------------------------------------------------------
# conversion_funnel
# ---------------------------------------------------------------------------


async def test_funnel_includes_converted_leads_the_d6_trap() -> None:
    """THE highest-value test in this sprint: a lead converted to a Contact
    must count in the funnel's converted bucket."""
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "converted", datetime(2026, 7, 3, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_conversion_funnel(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    converted_step = next(s for s in result.steps if s.stage == "converted")
    assert converted_step.count == 1


async def test_funnel_disqualified_reported_separately_not_as_a_step() -> None:
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "disqualified", datetime(2026, 7, 3, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_conversion_funnel(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    step_stages = [s.stage for s in result.steps]
    assert "disqualified" not in step_stages
    assert step_stages == ["captured", "qualified", "contacted", "converted"]
    assert result.disqualified_count == 1


async def test_funnel_drop_off_rate_and_overall_conversion_rate() -> None:
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "qualified", datetime(2026, 7, 3, tzinfo=UTC)),
        _lead("tenant-a", "qualified", datetime(2026, 7, 3, tzinfo=UTC)),
    ])
    claims = _claims("tenant-a")

    result = await get_conversion_funnel(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    captured_step = next(s for s in result.steps if s.stage == "captured")
    qualified_step = next(s for s in result.steps if s.stage == "qualified")
    assert captured_step.count == 4
    assert captured_step.drop_off_rate is None  # first step, no prior denominator
    assert qualified_step.count == 2
    # dropped 2 of 4 from captured -> qualified
    assert qualified_step.drop_off_rate == 0.5
    # overall_conversion_rate = converted(0) / captured(4)
    assert result.overall_conversion_rate == 0.0


async def test_funnel_zero_captured_gives_null_overall_conversion_rate() -> None:
    db = _StubDatabase(leads=[])
    claims = _claims("tenant-a")

    result = await get_conversion_funnel(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.overall_conversion_rate is None
    for step in result.steps:
        assert step.count == 0


async def test_funnel_rejects_global_caller() -> None:
    db = _StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_conversion_funnel(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_funnel_tenant_isolation_exact_values() -> None:
    db = _StubDatabase(leads=[
        _lead("tenant-a", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-a", "converted", datetime(2026, 7, 3, tzinfo=UTC)),
        _lead("tenant-b", "captured", datetime(2026, 7, 2, tzinfo=UTC)),
        _lead("tenant-b", "captured", datetime(2026, 7, 3, tzinfo=UTC)),
        _lead("tenant-b", "captured", datetime(2026, 7, 4, tzinfo=UTC)),
    ])

    result_a = await get_conversion_funnel(
        db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )
    result_b = await get_conversion_funnel(
        db, _claims("tenant-b"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO,
    )

    a_converted = next(s for s in result_a.steps if s.stage == "converted")
    b_captured = next(s for s in result_b.steps if s.stage == "captured")
    assert a_converted.count == 1
    assert b_captured.count == 3


# ---------------------------------------------------------------------------
# win_loss
# ---------------------------------------------------------------------------


def _opp(
    tenant_id: str,
    stage: str,
    *,
    closed_at: datetime | None,
    created_at: datetime,
    amount: Decimal | None,
    close_reason: str | None = None,
    owner_agent_id: str | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "stage": stage,
        "closed_at": closed_at,
        "created_at": created_at,
        "amount": amount,
        "close_reason": close_reason,
        "owner_agent_id": owner_agent_id,
    }


async def test_win_loss_counts_and_decimal_sums() -> None:
    db = _StubDatabase(opportunities=[
        _opp(
            "tenant-a", "closed_won",
            closed_at=datetime(2026, 7, 10, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=Decimal("12500.00"),
        ),
        _opp(
            "tenant-a", "closed_lost",
            closed_at=datetime(2026, 7, 12, tzinfo=UTC),
            created_at=datetime(2026, 7, 2, tzinfo=UTC),
            amount=None,
            close_reason="Went with a competitor",
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.won.count == 1
    assert result.won.amount_total == Decimal("12500.00")
    assert isinstance(result.won.amount_total, Decimal)
    assert result.lost.count == 1
    assert result.lost.amount_null_count == 1
    assert result.lost.avg_deal_size is None  # NULL amount excluded, never zeroed
    assert result.win_rate == 0.5
    assert len(result.loss_reasons) == 1
    assert result.loss_reasons[0].close_reason == "Went with a competitor"


async def test_win_loss_decimal_sum_is_exact_not_float_error() -> None:
    """19.99 + 0.01 would expose binary-float error if summed as float."""
    db = _StubDatabase(opportunities=[
        _opp(
            "tenant-a", "closed_won",
            closed_at=datetime(2026, 7, 5, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=Decimal("19.99"),
        ),
        _opp(
            "tenant-a", "closed_won",
            closed_at=datetime(2026, 7, 6, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=Decimal("0.01"),
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.won.amount_total == Decimal("20.00")


async def test_win_loss_uses_closed_at_not_created_at_or_expected_close_date() -> None:
    """An opportunity created inside the window but closed outside it must
    NOT appear; one created outside but closed inside it MUST appear."""
    db = _StubDatabase(opportunities=[
        _opp(
            "tenant-a", "closed_won",
            closed_at=datetime(2026, 6, 15, tzinfo=UTC),  # outside window
            created_at=datetime(2026, 7, 5, tzinfo=UTC),  # inside window
            amount=Decimal("100.00"),
        ),
        _opp(
            "tenant-a", "closed_won",
            closed_at=datetime(2026, 7, 20, tzinfo=UTC),  # inside window
            created_at=datetime(2026, 6, 1, tzinfo=UTC),  # outside window
            amount=Decimal("200.00"),
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.won.count == 1
    assert result.won.amount_total == Decimal("200.00")


async def test_win_loss_open_deal_counted_in_neither_bucket() -> None:
    db = _StubDatabase(opportunities=[
        _opp(
            "tenant-a", "prospecting",
            closed_at=None,
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
            amount=Decimal("100.00"),
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.won.count == 0
    assert result.lost.count == 0
    assert result.win_rate is None


async def test_win_loss_win_rate_null_when_both_zero() -> None:
    db = _StubDatabase(opportunities=[])
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.win_rate is None
    assert result.won.amount_total == Decimal("0")
    assert result.lost.amount_total == Decimal("0")


async def test_win_loss_loss_reasons_reverse_chronological_ungrouped() -> None:
    db = _StubDatabase(opportunities=[
        _opp(
            "tenant-a", "closed_lost",
            closed_at=datetime(2026, 7, 5, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=None,
            close_reason="Too expensive",
        ),
        _opp(
            "tenant-a", "closed_lost",
            closed_at=datetime(2026, 7, 15, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=None,
            close_reason="Too expensive",  # deliberately same text -- must NOT be grouped
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert len(result.loss_reasons) == 2  # not deduplicated/grouped
    assert result.loss_reasons[0].closed_at > result.loss_reasons[1].closed_at


async def test_win_loss_loss_reasons_capped_at_limit() -> None:
    db = _StubDatabase(opportunities=[
        _opp(
            "tenant-a", "closed_lost",
            closed_at=datetime(2026, 7, 1 + i, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=None,
            close_reason=f"reason-{i}",
        )
        for i in range(5)
    ])
    claims = _claims("tenant-a")

    result = await get_win_loss(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, reason_limit=2,
    )

    assert len(result.loss_reasons) == 2


async def test_win_loss_owner_agent_filter() -> None:
    db = _StubDatabase(opportunities=[
        _opp(
            "tenant-a", "closed_won",
            closed_at=datetime(2026, 7, 5, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=Decimal("500.00"),
            owner_agent_id="agent-1",
        ),
        _opp(
            "tenant-a", "closed_won",
            closed_at=datetime(2026, 7, 6, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            amount=Decimal("999.00"),
            owner_agent_id="agent-2",
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_win_loss(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, owner_agent_id="agent-1",
    )

    assert result.won.count == 1
    assert result.won.amount_total == Decimal("500.00")


async def test_win_loss_currency_configured_true_when_row_exists() -> None:
    db = _StubDatabase(
        opportunities=[],
        opportunity_configs={"tenant-a": {"currency": "EUR"}},
    )
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.currency == "EUR"
    assert result.currency_configured is True


async def test_win_loss_currency_configured_false_and_discloses_default() -> None:
    db = _StubDatabase(opportunities=[], opportunity_configs={})
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.currency_configured is False
    assert result.currency  # still states the (defaulted) code, not hidden


async def test_win_loss_win_probability_field_does_not_exist() -> None:
    """D13 -- win_probability must appear nowhere in this report."""
    db = _StubDatabase(opportunities=[])
    claims = _claims("tenant-a")

    result = await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert not hasattr(result, "win_probability")
    assert not hasattr(result.won, "win_probability")
    assert not hasattr(result.lost, "win_probability")


async def test_win_loss_rejects_global_caller() -> None:
    db = _StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_win_loss(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_win_loss_tenant_isolation_exact_values_including_currency() -> None:
    """MANDATORY: tenant B's win-loss reports B's currency, unaffected by A's config."""
    db = _StubDatabase(
        opportunities=[
            _opp(
                "tenant-a", "closed_won",
                closed_at=datetime(2026, 7, 5, tzinfo=UTC),
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                amount=Decimal("1000.00"),
            ),
            _opp(
                "tenant-b", "closed_won",
                closed_at=datetime(2026, 7, 5, tzinfo=UTC),
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                amount=Decimal("9999.00"),
            ),
            _opp(
                "tenant-b", "closed_won",
                closed_at=datetime(2026, 7, 6, tzinfo=UTC),
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                amount=Decimal("1.00"),
            ),
        ],
        opportunity_configs={"tenant-a": {"currency": "USD"}, "tenant-b": {"currency": "GBP"}},
    )

    result_a = await get_win_loss(db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    result_b = await get_win_loss(db, _claims("tenant-b"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result_a.won.amount_total == Decimal("1000.00")
    assert result_a.currency == "USD"
    assert result_b.won.amount_total == Decimal("10000.00")
    assert result_b.currency == "GBP"


# ---------------------------------------------------------------------------
# SR-19: lead_sources, score_distribution, agent_performance,
# recent_conversions -- four new read-only aggregates over `leads` (D1/D2).
# Each takes AuthClaims first, calls _reject_global, filters
# WHERE tenant_id = $1, single-table over `leads` only.
# ---------------------------------------------------------------------------

from api.analytics.reports_repository import (  # noqa: E402
    get_agent_performance,
    get_lead_sources,
    get_recent_conversions,
    get_score_distribution,
)


def _sr19_lead(
    tenant_id: str,
    *,
    stage: str = "captured",
    created_at: datetime,
    updated_at: datetime | None = None,
    source: str = "widget",
    qualification_score: int | None = None,
    assigned_agent_id: str | None = None,
    name: str = "Jane Doe",
    lead_id: str = "lead-1",
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "stage": stage,
        "created_at": created_at,
        "updated_at": updated_at if updated_at is not None else created_at,
        "source": source,
        "qualification_score": qualification_score,
        "assigned_agent_id": assigned_agent_id,
        "name": name,
    }


class _Sr19StubDatabase:
    """Stub DB for the four SR-19 aggregates -- models the extra `leads`
    columns (`qualification_score`, `updated_at`, `name`,
    `converted_to_contact_id`) that the shared `_StubDatabase` above does
    not need for the SR-9.5 reports. Dispatch is by substring match on the
    real SQL text, exercising the real query shape (tenant filter, window,
    GROUP BY/FILTER) without a live DB, matching this file's established
    convention.
    """

    def __init__(self, *, leads: list[dict[str, Any]] | None = None) -> None:
        self.leads = leads or []

    async def execute(self, query: str, *args: Any) -> str:
        raise AssertionError("Reports repository must not issue writes")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.upper()
        if "FROM LEADS" not in q or "FILTER (WHERE QUALIFICATION_SCORE" not in q:
            raise AssertionError(f"Unexpected fetchrow query: {query}")

        tenant_id, window_from, window_to = args[0], args[1], args[2]
        rows = [
            r for r in self.leads
            if r["tenant_id"] == tenant_id and window_from <= r["created_at"] < window_to
        ]
        band_0_19 = sum(1 for r in rows if r["qualification_score"] is not None and 0 <= r["qualification_score"] <= 19)
        band_20_39 = sum(1 for r in rows if r["qualification_score"] is not None and 20 <= r["qualification_score"] <= 39)
        band_40_59 = sum(1 for r in rows if r["qualification_score"] is not None and 40 <= r["qualification_score"] <= 59)
        band_60_79 = sum(1 for r in rows if r["qualification_score"] is not None and 60 <= r["qualification_score"] <= 79)
        band_80_100 = sum(1 for r in rows if r["qualification_score"] is not None and 80 <= r["qualification_score"] <= 100)
        unscored = sum(1 for r in rows if r["qualification_score"] is None)
        return {
            "band_0_19": band_0_19, "band_20_39": band_20_39, "band_40_59": band_40_59,
            "band_60_79": band_60_79, "band_80_100": band_80_100, "unscored": unscored,
        }

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.upper()
        if "FROM LEADS" not in q:
            raise AssertionError(f"Unexpected fetch query: {query}")

        tenant_id, window_from, window_to = args[0], args[1], args[2]

        if "WHERE TENANT_ID = $1 AND STAGE = 'CONVERTED'" in q:
            # Recent conversions: filtered on stage='converted' AND
            # updated_at in window -- must NOT inherit include_converted's
            # AND converted_to_contact_id IS NULL default (D6/M6).
            rows = [
                r for r in self.leads
                if r["tenant_id"] == tenant_id
                and r["stage"] == "converted"
                and window_from <= r["updated_at"] < window_to
            ]
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
            limit = args[3]
            rows = rows[:limit]
            return [
                {
                    "lead_id": r["lead_id"], "name": r["name"], "source": r["source"],
                    "stage": r["stage"], "converted_at": r["updated_at"],
                }
                for r in rows
            ]

        # All other SR-19 aggregates window on created_at.
        rows = [
            r for r in self.leads
            if r["tenant_id"] == tenant_id and window_from <= r["created_at"] < window_to
        ]

        if "GROUP BY SOURCE" in q:
            grouped: dict[str, int] = {}
            for r in rows:
                grouped[r["source"]] = grouped.get(r["source"], 0) + 1
            return [{"source": k, "cnt": v} for k, v in grouped.items()]

        if "GROUP BY ASSIGNED_AGENT_ID" in q:
            grouped_agents: dict[str | None, dict[str, int]] = {}
            for r in rows:
                slot = grouped_agents.setdefault(
                    r["assigned_agent_id"], {"assigned": 0, "contacted": 0, "won": 0},
                )
                slot["assigned"] += 1
                if r["stage"] in ("contacted", "converted"):
                    slot["contacted"] += 1
                if r["stage"] == "converted":
                    slot["won"] += 1
            return [
                {"assigned_agent_id": k, **v} for k, v in grouped_agents.items()
            ]

        raise AssertionError(f"Unexpected leads query shape (SR-19): {query}")


# ---------------------------------------------------------------------------
# get_lead_sources (D4)
# ---------------------------------------------------------------------------


async def test_lead_sources_single_source_not_padded_and_flagged() -> None:
    """D4 -- a tenant with only 'widget' leads gets ONE entry, never four
    padded entries with three zeros. single_source is True."""
    db = _Sr19StubDatabase(leads=[
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), source="widget"),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 3, tzinfo=UTC), source="widget"),
    ])
    claims = _claims("tenant-a")

    result = await get_lead_sources(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert len(result.sources) == 1
    assert result.sources[0].source == "widget"
    assert result.sources[0].count == 2
    assert result.sources[0].percentage == 100.0
    assert result.total == 2
    assert result.single_source is True


async def test_lead_sources_multiple_sources_not_single_flagged() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), source="widget"),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 3, tzinfo=UTC), source="referral"),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 4, tzinfo=UTC), source="referral"),
    ])
    claims = _claims("tenant-a")

    result = await get_lead_sources(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.single_source is False
    assert result.total == 3
    by_source = {s.source: s.count for s in result.sources}
    assert by_source == {"widget": 1, "referral": 2}


async def test_lead_sources_empty_window_zero_total_not_single_source() -> None:
    db = _Sr19StubDatabase(leads=[])
    claims = _claims("tenant-a")

    result = await get_lead_sources(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.sources == []
    assert result.total == 0
    assert result.single_source is False


async def test_lead_sources_rejects_global_caller() -> None:
    db = _Sr19StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_lead_sources(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_lead_sources_tenant_isolation_exact_values() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), source="widget"),
        _sr19_lead("tenant-b", created_at=datetime(2026, 7, 2, tzinfo=UTC), source="widget"),
        _sr19_lead("tenant-b", created_at=datetime(2026, 7, 3, tzinfo=UTC), source="referral"),
    ])

    result_a = await get_lead_sources(db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    result_b = await get_lead_sources(db, _claims("tenant-b"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result_a.total == 1
    assert result_a.single_source is True
    assert result_b.total == 2
    assert result_b.single_source is False


# ---------------------------------------------------------------------------
# get_score_distribution (D8)
# ---------------------------------------------------------------------------


async def test_score_distribution_all_five_bands_present_when_empty() -> None:
    db = _Sr19StubDatabase(leads=[])
    claims = _claims("tenant-a")

    result = await get_score_distribution(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.bands == {
        "0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0,
    }
    assert result.unscored == 0
    assert result.total == 0


async def test_score_distribution_null_score_counted_as_unscored_not_0_19() -> None:
    """D8 -- a NULL qualification_score is 'unscored', NEVER bucketed into
    0-19. This is the highest-value no-silent-fallback test for this report."""
    db = _Sr19StubDatabase(leads=[
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=None),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 3, tzinfo=UTC), qualification_score=5),
    ])
    claims = _claims("tenant-a")

    result = await get_score_distribution(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.unscored == 1
    assert result.bands["0-19"] == 1
    assert result.total == 2


async def test_score_distribution_fixed_20_point_bands() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=0),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=19),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=20),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=39),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=40),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=59),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=60),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=79),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=80),
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=100),
    ])
    claims = _claims("tenant-a")

    result = await get_score_distribution(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.bands == {
        "0-19": 2, "20-39": 2, "40-59": 2, "60-79": 2, "80-100": 2,
    }
    assert result.unscored == 0
    assert result.total == 10


async def test_score_distribution_rejects_global_caller() -> None:
    db = _Sr19StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_score_distribution(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_score_distribution_tenant_isolation_exact_values() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead("tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=10),
        _sr19_lead("tenant-b", created_at=datetime(2026, 7, 2, tzinfo=UTC), qualification_score=90),
        _sr19_lead("tenant-b", created_at=datetime(2026, 7, 3, tzinfo=UTC), qualification_score=None),
    ])

    result_a = await get_score_distribution(db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    result_b = await get_score_distribution(db, _claims("tenant-b"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result_a.bands["0-19"] == 1
    assert result_a.total == 1
    assert result_b.bands["80-100"] == 1
    assert result_b.unscored == 1
    assert result_b.total == 2


# ---------------------------------------------------------------------------
# get_agent_performance (D7)
# ---------------------------------------------------------------------------


async def test_agent_performance_zero_denominator_win_rate_is_null_not_zero() -> None:
    """D7 -- an agent with zero assigned leads must never appear as 0%; this
    aggregate only reports agents present in the window, so a zero-lead
    agent simply cannot appear here (the UI's in-memory join is what
    surfaces the 'No data' state for a listed-but-unassigned agent)."""
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC),
            stage="captured", assigned_agent_id="agent-1",
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_agent_performance(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    agent_1 = next(a for a in result.agents if a.assigned_agent_id == "agent-1")
    assert agent_1.assigned == 1
    assert agent_1.won == 0
    assert agent_1.win_rate is None  # zero denominator (0 contacted) -- null, not 0


async def test_agent_performance_win_rate_computed_when_contacted_nonzero() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC),
            stage="contacted", assigned_agent_id="agent-1",
        ),
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 3, tzinfo=UTC),
            stage="converted", assigned_agent_id="agent-1",
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_agent_performance(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    agent_1 = next(a for a in result.agents if a.assigned_agent_id == "agent-1")
    assert agent_1.assigned == 2
    assert agent_1.contacted == 2  # contacted + converted both count as "reached contacted"
    assert agent_1.won == 1
    assert agent_1.win_rate == 0.5


async def test_agent_performance_unassigned_leads_appear_as_explicit_row() -> None:
    """D7 -- assigned_agent_id IS NULL leads are their own explicit
    'Unassigned' row, never dropped; counts sum to the tenant's total."""
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC),
            stage="captured", assigned_agent_id=None,
        ),
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 3, tzinfo=UTC),
            stage="qualified", assigned_agent_id="agent-1",
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_agent_performance(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.unassigned.assigned == 1
    total_assigned = sum(a.assigned for a in result.agents) + result.unassigned.assigned
    assert total_assigned == 2


async def test_agent_performance_never_lists_another_tenants_agent_id() -> None:
    """The highest-value isolation test in this sprint -- an agent id is a
    recognisable identifier belonging to another business."""
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC),
            stage="captured", assigned_agent_id="agent-a-1",
        ),
        _sr19_lead(
            "tenant-b", created_at=datetime(2026, 7, 2, tzinfo=UTC),
            stage="captured", assigned_agent_id="agent-b-secret",
        ),
    ])

    result_a = await get_agent_performance(db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    agent_ids = {a.assigned_agent_id for a in result_a.agents}
    assert "agent-b-secret" not in agent_ids
    assert agent_ids == {"agent-a-1"}


async def test_agent_performance_won_le_contacted_le_assigned() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 2, tzinfo=UTC),
            stage="captured", assigned_agent_id="agent-1",
        ),
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 3, tzinfo=UTC),
            stage="contacted", assigned_agent_id="agent-1",
        ),
        _sr19_lead(
            "tenant-a", created_at=datetime(2026, 7, 4, tzinfo=UTC),
            stage="converted", assigned_agent_id="agent-1",
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_agent_performance(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    agent_1 = next(a for a in result.agents if a.assigned_agent_id == "agent-1")
    assert agent_1.won <= agent_1.contacted <= agent_1.assigned


async def test_agent_performance_rejects_global_caller() -> None:
    db = _Sr19StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_agent_performance(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# get_recent_conversions (D6 -- the M6 trap)
# ---------------------------------------------------------------------------


async def test_recent_conversions_includes_converted_leads_the_d6_trap() -> None:
    """THE highest-value test in this sprint's fourth report: a naive query
    copying list_leads' include_converted=False default would ALWAYS return
    zero rows. This must NOT happen."""
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", lead_id="lead-1", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_recent_conversions(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert len(result.conversions) == 1
    assert result.conversions[0].lead_id == "lead-1"


async def test_recent_conversions_windows_on_updated_at_not_created_at() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", lead_id="lead-1", stage="converted",
            created_at=datetime(2026, 6, 1, tzinfo=UTC),  # outside window
            updated_at=datetime(2026, 7, 10, tzinfo=UTC),  # inside window
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_recent_conversions(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert len(result.conversions) == 1


async def test_recent_conversions_excludes_non_converted_stages() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", lead_id="lead-1", stage="qualified",
            created_at=datetime(2026, 7, 2, tzinfo=UTC),
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_recent_conversions(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert result.conversions == []


async def test_recent_conversions_reverse_chronological() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", lead_id="lead-early", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        ),
        _sr19_lead(
            "tenant-a", lead_id="lead-late", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 20, tzinfo=UTC),
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_recent_conversions(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert [c.lead_id for c in result.conversions] == ["lead-late", "lead-early"]


async def test_recent_conversions_no_value_field_anywhere() -> None:
    """D6/M5 -- a lead has no monetary value; asserted by absence."""
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", lead_id="lead-1", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
    ])
    claims = _claims("tenant-a")

    result = await get_recent_conversions(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    conversion = result.conversions[0]
    assert not hasattr(conversion, "value")


async def test_recent_conversions_respects_limit() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", lead_id=f"lead-{i}", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 1 + i, tzinfo=UTC),
        )
        for i in range(5)
    ])
    claims = _claims("tenant-a")

    result = await get_recent_conversions(
        db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO, limit=2,
    )

    assert len(result.conversions) == 2


async def test_recent_conversions_rejects_global_caller() -> None:
    db = _Sr19StubDatabase()
    claims = _claims(None, role=Role.PLATFORM_ADMIN)

    with pytest.raises(ValidationError) as exc_info:
        await get_recent_conversions(db, claims, window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


async def test_recent_conversions_tenant_isolation_exact_values() -> None:
    db = _Sr19StubDatabase(leads=[
        _sr19_lead(
            "tenant-a", lead_id="lead-a-1", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        ),
        _sr19_lead(
            "tenant-b", lead_id="lead-b-1", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        ),
        _sr19_lead(
            "tenant-b", lead_id="lead-b-2", stage="converted",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            updated_at=datetime(2026, 7, 6, tzinfo=UTC),
        ),
    ])

    result_a = await get_recent_conversions(db, _claims("tenant-a"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)
    result_b = await get_recent_conversions(db, _claims("tenant-b"), window_from=_WINDOW_FROM, window_to=_WINDOW_TO)

    assert [c.lead_id for c in result_a.conversions] == ["lead-a-1"]
    assert {c.lead_id for c in result_b.conversions} == {"lead-b-1", "lead-b-2"}
