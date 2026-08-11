"""Fixed CRM reports -- tenant-scoped aggregate SQL over ``leads``,
``schedule_events`` and ``opportunities`` (SR-9.5).

Follows ``analytics/repository.py``'s DELIBERATE precedent (D2): this
module owns its own aggregate SQL directly against the owning modules'
tables. It does **not** import or call ``leads.repository``,
``scheduling.repository`` or ``opportunities.repository`` -- those are
row-level, tenant+visitor CRUD helpers; these are tenant-wide ``GROUP BY``
aggregates, a genuinely different query shape (see that module's docstring
for the full argument). The one sanctioned cross-module call is
``opportunities.config_repository.get_opportunity_config`` for the
tenant's currency -- a config *resolver*, not an aggregate.

Every function:
- Takes ``AuthClaims`` first and calls ``_reject_global(claims)`` --
  PLATFORM_ADMIN (no ``tenant_id``) is always rejected.
- Filters ``WHERE tenant_id = $1`` and reads exactly ONE table (D11) -- no
  cross-entity JOIN in this increment.
- Uses positional placeholders (``$1``, ``$2``, ...), never string-
  interpolated values.
- Reuses ``analytics.repository._round_rate`` for every rate: a
  zero-denominator rate is ``None`` (JSON ``null``), never a fabricated
  ``0.0`` (D12).

Critical correctness note (D6/M7 -- the trap this sprint exists to defuse):
``leads_by_stage`` and ``conversion_funnel`` deliberately do NOT filter out
converted (tombstoned) leads. ``leads.repository.list_leads`` defaults to
``include_converted=False`` because the *working list* should hide
tombstones by design -- but a report reusing that default would silently
report ``converted = 0`` for every tenant that actually converts leads.
These queries never add an ``AND converted_to_contact_id IS NULL`` clause.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.analytics.repository import _round_rate
from api.leads.pipeline import STAGE_ORDER as LEAD_STAGE_ORDER
from api.opportunities.config_repository import get_opportunity_config

_DISQUALIFIED_STAGE = "disqualified"
_ALL_LEAD_STAGE_KEYS: tuple[str, ...] = (*LEAD_STAGE_ORDER, _DISQUALIFIED_STAGE)

_BOOKING_STATUSES: tuple[str, ...] = ("booked", "completed", "no_show", "cancelled")

_DEFAULT_LOSS_REASON_LIMIT = 50


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Every report is tenant-scoped; a global caller has no ``tenant_id`` and
    therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Reports are tenant-scoped.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


# ---------------------------------------------------------------------------
# Leads by stage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadsByStage:
    """Per-stage lead counts, every ``STAGE_ORDER`` key + 'disqualified'
    always present (explicit 0 when a tenant has no leads at that stage)."""

    stages: dict[str, int]
    total: int


async def get_leads_by_stage(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
    source: str | None = None,
    assigned_agent_id: str | None = None,
) -> LeadsByStage:
    """``GROUP BY stage`` over ``leads`` in ``[window_from, window_to)``.

    Includes converted (tombstoned) leads -- does NOT filter on
    ``converted_to_contact_id`` (D6/M7). Every key in ``STAGE_ORDER`` plus
    ``"disqualified"`` is present in the result with an explicit ``0`` when
    a tenant has no leads at that stage.
    """
    _reject_global(claims)

    where = "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3"
    params: list[Any] = [claims.tenant_id, window_from, window_to]

    if source is not None:
        params.append(source)
        where += f" AND source = ${len(params)}"
    if assigned_agent_id is not None:
        params.append(assigned_agent_id)
        where += f" AND assigned_agent_id = ${len(params)}"

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    rows = await db.fetch(
        "SELECT stage, count(*) AS cnt FROM leads " + where + " GROUP BY stage",
        *params,
    )

    stages: dict[str, int] = dict.fromkeys(_ALL_LEAD_STAGE_KEYS, 0)
    total = 0
    for row in rows:
        stage = str(row["stage"])
        cnt = int(row["cnt"])
        if stage in stages:
            stages[stage] = cnt
        total += cnt

    return LeadsByStage(stages=stages, total=total)


# ---------------------------------------------------------------------------
# Bookings series
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookingsBucket:
    """One time-bucketed slice of the bookings series."""

    bucket_start: datetime
    booked: int
    completed: int
    no_show: int
    cancelled: int
    total_excluding_cancelled: int


@dataclass(frozen=True)
class BookingsSeries:
    """Bucketed bookings report -- series + window totals (same keys)."""

    series: list[BookingsBucket]
    totals: BookingsBucket


async def get_bookings_series(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
    bucket: str,
    source: str | None = None,
    status: str | None = None,
) -> BookingsSeries:
    """Per-bucket booking counts by ``created_at`` (when booked), NOT
    ``starts_at`` (D5). Counts ALL statuses -- ``cancelled`` is its own
    visible series value, ``completed``/``no_show`` are counted (never
    dropped), unlike the legacy ``overview`` series' ``status='booked'``
    narrowing (M8), which this report deliberately does not repeat.

    ``total_excluding_cancelled`` = ``booked + completed + no_show`` for
    that bucket (cancellations are visible but excluded from the "real
    activity" total).
    """
    _reject_global(claims)

    where = "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3"
    params: list[Any] = [claims.tenant_id, window_from, window_to]

    if source is not None:
        params.append(source)
        where += f" AND source = ${len(params)}"
    if status is not None:
        params.append(status)
        where += f" AND status = ${len(params)}"

    params.append(bucket)
    bucket_idx = len(params)

    # Parameterized SQL; `where` is a safe constant clause built above.
    # `bucket` reaches date_trunc as a bound parameter, never concatenated.
    # ruff: noqa: S608
    rows = await db.fetch(
        f"SELECT date_trunc(${bucket_idx}::text, created_at) AS bucket, "
        "count(*) FILTER (WHERE status = 'booked')    AS booked, "
        "count(*) FILTER (WHERE status = 'completed') AS completed, "
        "count(*) FILTER (WHERE status = 'no_show')   AS no_show, "
        "count(*) FILTER (WHERE status = 'cancelled') AS cancelled "
        "FROM schedule_events " + where + " GROUP BY 1 ORDER BY 1",
        *params,
    )

    series: list[BookingsBucket] = []
    total_booked = 0
    total_completed = 0
    total_no_show = 0
    total_cancelled = 0

    for row in rows:
        booked = int(row["booked"])
        completed = int(row["completed"])
        no_show = int(row["no_show"])
        cancelled = int(row["cancelled"])
        series.append(
            BookingsBucket(
                bucket_start=row["bucket"],
                booked=booked,
                completed=completed,
                no_show=no_show,
                cancelled=cancelled,
                total_excluding_cancelled=booked + completed + no_show,
            )
        )
        total_booked += booked
        total_completed += completed
        total_no_show += no_show
        total_cancelled += cancelled

    totals = BookingsBucket(
        bucket_start=window_from,
        booked=total_booked,
        completed=total_completed,
        no_show=total_no_show,
        cancelled=total_cancelled,
        total_excluding_cancelled=total_booked + total_completed + total_no_show,
    )

    return BookingsSeries(series=series, totals=totals)


# ---------------------------------------------------------------------------
# Conversion funnel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunnelStep:
    """One funnel step -- current-stage count + drop-off rate from the
    previous step (``None`` for the first step or a zero denominator)."""

    stage: str
    count: int
    drop_off_rate: float | None


@dataclass(frozen=True)
class ConversionFunnel:
    """The leads-only conversion funnel (D6): captured -> qualified ->
    contacted -> converted, plus disqualified as a separate off-ramp
    count (NOT a funnel step)."""

    steps: list[FunnelStep]
    disqualified_count: int
    overall_conversion_rate: float | None


async def get_conversion_funnel(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
    source: str | None = None,
) -> ConversionFunnel:
    """Leads-only funnel over current ``stage`` (D6), including converted
    (tombstoned) leads -- does NOT filter on ``converted_to_contact_id``
    (D6/M7, the critical correctness requirement of this sprint).
    ``disqualified`` is reported separately as an off-ramp, never as a
    fifth funnel step. Per-step ``drop_off_rate`` via ``_round_rate``: the
    fraction of the PREVIOUS step's count that did NOT reach this step.
    """
    _reject_global(claims)

    where = "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3"
    params: list[Any] = [claims.tenant_id, window_from, window_to]

    if source is not None:
        params.append(source)
        where += f" AND source = ${len(params)}"

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    rows = await db.fetch(
        "SELECT stage, count(*) AS cnt FROM leads " + where + " GROUP BY stage",
        *params,
    )

    counts: dict[str, int] = dict.fromkeys(_ALL_LEAD_STAGE_KEYS, 0)
    for row in rows:
        stage = str(row["stage"])
        if stage in counts:
            counts[stage] = int(row["cnt"])

    steps: list[FunnelStep] = []
    previous_count: int | None = None
    for stage in LEAD_STAGE_ORDER:
        count = counts[stage]
        if previous_count is None:
            drop_off_rate: float | None = None
        else:
            dropped = max(previous_count - count, 0)
            drop_off_rate = _round_rate(dropped, previous_count)
        steps.append(FunnelStep(stage=stage, count=count, drop_off_rate=drop_off_rate))
        previous_count = count

    captured_count = counts[LEAD_STAGE_ORDER[0]]
    converted_count = counts[LEAD_STAGE_ORDER[-1]]

    return ConversionFunnel(
        steps=steps,
        disqualified_count=counts[_DISQUALIFIED_STAGE],
        overall_conversion_rate=_round_rate(converted_count, captured_count),
    )


# ---------------------------------------------------------------------------
# Win / loss
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WinLossOutcome:
    """Counts/totals for one outcome (won or lost)."""

    count: int
    amount_total: Decimal
    amount_null_count: int
    avg_deal_size: Decimal | None
    avg_days_to_close: float | None


@dataclass(frozen=True)
class LossReason:
    """One raw, ungrouped loss reason (D13 -- never grouped/counted)."""

    closed_at: datetime
    close_reason: str | None


@dataclass(frozen=True)
class WinLossReport:
    """The win/loss report -- won/lost outcomes, win_rate, ungrouped
    reverse-chronological loss reasons, and the tenant's currency."""

    currency: str
    currency_configured: bool
    won: WinLossOutcome
    lost: WinLossOutcome
    win_rate: float | None
    loss_reasons: list[LossReason] = field(default_factory=list)


async def get_win_loss(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
    owner_agent_id: str | None = None,
    reason_limit: int = _DEFAULT_LOSS_REASON_LIMIT,
) -> WinLossReport:
    """Aggregate over ``opportunities`` whose ``closed_at`` falls in
    ``[window_from, window_to)`` (NOT ``created_at``/``expected_close_date``,
    D13/M14). ``SUM(amount)``/``AVG`` return ``Decimal`` -- NULL amounts are
    EXCLUDED (never coerced to 0), with ``amount_null_count`` reported.
    ``avg_days_to_close`` is the mean of ``closed_at - created_at`` in days.
    ``win_rate = won / (won + lost)``, ``None`` when both are 0.
    ``loss_reasons`` is a raw, reverse-chronological, capped, UNGROUPED list
    -- never a taxonomy (D13). ``win_probability`` never appears here.
    """
    _reject_global(claims)

    where = (
        "WHERE tenant_id = $1 AND stage IN ('closed_won', 'closed_lost') "
        "AND closed_at >= $2 AND closed_at < $3"
    )
    params: list[Any] = [claims.tenant_id, window_from, window_to]

    if owner_agent_id is not None:
        params.append(owner_agent_id)
        where += f" AND owner_agent_id = ${len(params)}"

    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    agg_rows = await db.fetch(
        "SELECT stage, "
        "count(*) AS cnt, "
        "count(*) FILTER (WHERE amount IS NULL) AS amount_null_count, "
        "sum(amount) FILTER (WHERE amount IS NOT NULL) AS amount_total, "
        "avg(EXTRACT(EPOCH FROM (closed_at - created_at)) / 86400.0) AS avg_days_to_close "
        "FROM opportunities " + where + " GROUP BY stage",
        *params,
    )

    outcomes: dict[str, WinLossOutcome] = {}
    for row in agg_rows:
        stage = str(row["stage"])
        cnt = int(row["cnt"])
        amount_null_count = int(row["amount_null_count"])
        amount_total = row["amount_total"] if row["amount_total"] is not None else Decimal("0")
        quoted_count = cnt - amount_null_count
        avg_deal_size = (amount_total / quoted_count) if quoted_count > 0 else None
        avg_days_raw = row["avg_days_to_close"]
        avg_days_to_close = float(avg_days_raw) if avg_days_raw is not None else None

        outcomes[stage] = WinLossOutcome(
            count=cnt,
            amount_total=amount_total,
            amount_null_count=amount_null_count,
            avg_deal_size=avg_deal_size,
            avg_days_to_close=avg_days_to_close,
        )

    won = outcomes.get(
        "closed_won",
        WinLossOutcome(
            count=0, amount_total=Decimal("0"), amount_null_count=0,
            avg_deal_size=None, avg_days_to_close=None,
        ),
    )
    lost = outcomes.get(
        "closed_lost",
        WinLossOutcome(
            count=0, amount_total=Decimal("0"), amount_null_count=0,
            avg_deal_size=None, avg_days_to_close=None,
        ),
    )

    win_rate = _round_rate(won.count, won.count + lost.count)

    reason_params = list(params)
    reason_params.append(max(1, reason_limit))
    limit_idx = len(reason_params)
    # Parameterized SQL; `where` is a safe constant clause built above.
    # ruff: noqa: S608
    reason_rows = await db.fetch(
        "SELECT closed_at, close_reason FROM opportunities " + where +
        f" AND stage = 'closed_lost' ORDER BY closed_at DESC LIMIT ${limit_idx}",
        *reason_params,
    )
    loss_reasons = [
        LossReason(closed_at=row["closed_at"], close_reason=row["close_reason"])
        for row in reason_rows
    ]

    config = await get_opportunity_config(db, claims)
    settings_currency_configured = await _is_currency_configured(db, claims)

    return WinLossReport(
        currency=config.currency,
        currency_configured=settings_currency_configured,
        won=won,
        lost=lost,
        win_rate=win_rate,
        loss_reasons=loss_reasons,
    )



# ---------------------------------------------------------------------------
# SR-19: lead sources, score distribution, agent performance, recent
# conversions -- four new read-only aggregates over `leads` ONLY (D1/D2).
# Each takes AuthClaims first, calls _reject_global before any SQL, filters
# WHERE tenant_id = $1, and is single-table -- no cross-entity JOIN.
# ---------------------------------------------------------------------------

_SCORE_BAND_LABELS: tuple[str, ...] = ("0-19", "20-39", "40-59", "60-79", "80-100")


@dataclass(frozen=True)
class LeadSource:
    """One real source and its share of the window's leads."""

    source: str
    count: int
    percentage: float


@dataclass(frozen=True)
class LeadSourcesReport:
    """Lead-sources donut data (D4).

    `sources` carries WHATEVER real source strings exist in the window --
    NEVER padded with the mockup's four categories and zero counts (D4). A
    real tenant is expected to show ONE entry ('widget') for a long time,
    because `leads.source` defaults to `'widget'` (M4) and the widget is
    currently the platform's only lead-creation channel. That is expected
    behaviour, not a bug -- do NOT "fix" a single-slice result by padding
    variety or hardcoding a taxonomy the data does not have. `single_source`
    is the UI's flag to show an honest note instead of looking broken.
    """

    sources: list[LeadSource]
    total: int
    single_source: bool


async def get_lead_sources(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
) -> LeadSourcesReport:
    """``GROUP BY source`` over ``leads`` in ``[window_from, window_to)``.

    See ``LeadSourcesReport``'s docstring (D4) -- a single-slice result for
    a real tenant is the expected, correct output, not a defect.
    """
    _reject_global(claims)

    # Parameterized SQL; single-table over `leads`. ruff: noqa: S608
    rows = await db.fetch(
        "SELECT source, count(*) AS cnt FROM leads "
        "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3 "
        "GROUP BY source",
        claims.tenant_id, window_from, window_to,
    )

    total = sum(int(row["cnt"]) for row in rows)

    def _percentage(count: int) -> float:
        rate = _round_rate(count, total)
        return rate * 100 if rate is not None else 0.0

    sources = [
        LeadSource(
            source=str(row["source"]),
            count=int(row["cnt"]),
            percentage=_percentage(int(row["cnt"])),
        )
        for row in rows
    ]
    # Deterministic order (largest first) -- makes CSV/JSON output stable.
    sources.sort(key=lambda s: (-s.count, s.source))

    return LeadSourcesReport(sources=sources, total=total, single_source=len(sources) == 1)


@dataclass(frozen=True)
class ScoreDistributionReport:
    """Fixed 20-point score bands (D8), always all five present with an
    explicit 0 when empty, plus a separate `unscored` count.
    `qualification_score IS NULL` is NEVER bucketed into `0-19` -- a lead
    nobody scored is not a lead that scored badly."""

    bands: dict[str, int]
    unscored: int
    total: int


async def get_score_distribution(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
) -> ScoreDistributionReport:
    """Fixed 20-point ``qualification_score`` bands (D8) over ``leads`` in
    ``[window_from, window_to)``. ``NULL`` scores are counted separately as
    ``unscored``, never folded into ``0-19``.
    """
    _reject_global(claims)

    # Parameterized SQL; single-table over `leads`. ruff: noqa: S608
    row = await db.fetchrow(
        "SELECT "
        "count(*) FILTER (WHERE qualification_score BETWEEN 0 AND 19)   AS band_0_19, "
        "count(*) FILTER (WHERE qualification_score BETWEEN 20 AND 39)  AS band_20_39, "
        "count(*) FILTER (WHERE qualification_score BETWEEN 40 AND 59)  AS band_40_59, "
        "count(*) FILTER (WHERE qualification_score BETWEEN 60 AND 79)  AS band_60_79, "
        "count(*) FILTER (WHERE qualification_score BETWEEN 80 AND 100) AS band_80_100, "
        "count(*) FILTER (WHERE qualification_score IS NULL)            AS unscored "
        "FROM leads "
        "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3",
        claims.tenant_id, window_from, window_to,
    )

    if row is None:
        bands = dict.fromkeys(_SCORE_BAND_LABELS, 0)
        return ScoreDistributionReport(bands=bands, unscored=0, total=0)

    bands = {
        "0-19": int(row["band_0_19"]),
        "20-39": int(row["band_20_39"]),
        "40-59": int(row["band_40_59"]),
        "60-79": int(row["band_60_79"]),
        "80-100": int(row["band_80_100"]),
    }
    unscored = int(row["unscored"])
    total = sum(bands.values()) + unscored

    return ScoreDistributionReport(bands=bands, unscored=unscored, total=total)


@dataclass(frozen=True)
class AgentPerformanceRow:
    """Per-agent (or explicit Unassigned) counts. `win_rate` is `None` at a
    zero `contacted` denominator -- NEVER a fabricated `0` (D7)."""

    assigned_agent_id: str | None
    assigned: int
    contacted: int
    won: int
    win_rate: float | None


@dataclass(frozen=True)
class AgentPerformanceReport:
    """Agent performance over `leads` only -- names are resolved by the
    caller (admin-web), NEVER by a SQL JOIN and NEVER by one request per
    agent (D7). `unassigned` is always present as its own explicit row so
    the table's counts sum to the tenant's total in the window."""

    agents: list[AgentPerformanceRow]
    unassigned: AgentPerformanceRow


async def get_agent_performance(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
) -> AgentPerformanceReport:
    """``GROUP BY assigned_agent_id`` over ``leads`` in
    ``[window_from, window_to)`` (D7). ``contacted`` counts leads that
    reached at least ``contacted`` in the funnel (``stage`` IN
    ``('contacted', 'converted')``); ``won`` counts ``stage = 'converted'``.
    ``win_rate = won / contacted`` via ``_round_rate`` -- ``None`` when
    ``contacted`` is 0, never a fabricated ``0``. ``assigned_agent_id IS
    NULL`` leads are aggregated separately and returned as ``unassigned``,
    never dropped.
    """
    _reject_global(claims)

    # Parameterized SQL; single-table over `leads`. ruff: noqa: S608
    rows = await db.fetch(
        "SELECT assigned_agent_id, "
        "count(*) AS assigned, "
        "count(*) FILTER (WHERE stage IN ('contacted', 'converted')) AS contacted, "
        "count(*) FILTER (WHERE stage = 'converted') AS won "
        "FROM leads "
        "WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3 "
        "GROUP BY assigned_agent_id",
        claims.tenant_id, window_from, window_to,
    )

    agents: list[AgentPerformanceRow] = []
    unassigned = AgentPerformanceRow(
        assigned_agent_id=None, assigned=0, contacted=0, won=0, win_rate=None,
    )

    for row in rows:
        assigned = int(row["assigned"])
        contacted = int(row["contacted"])
        won = int(row["won"])
        win_rate = _round_rate(won, contacted)
        agent_row = AgentPerformanceRow(
            assigned_agent_id=row["assigned_agent_id"],
            assigned=assigned, contacted=contacted, won=won, win_rate=win_rate,
        )
        if row["assigned_agent_id"] is None:
            unassigned = agent_row
        else:
            agents.append(agent_row)

    agents.sort(key=lambda a: a.assigned_agent_id or "")

    return AgentPerformanceReport(agents=agents, unassigned=unassigned)


@dataclass(frozen=True)
class RecentConversion:
    """One converted lead. Deliberately NO monetary value field (D6/M5) --
    money lives on `opportunities.amount`, a different entity behind a
    cross-entity JOIN this module is forbidden from making."""

    lead_id: str
    name: str
    source: str
    stage: str
    converted_at: datetime


@dataclass(frozen=True)
class RecentConversionsReport:
    conversions: list[RecentConversion]


_DEFAULT_RECENT_CONVERSIONS_LIMIT = 50


async def get_recent_conversions(
    db: Database,
    claims: AuthClaims,
    *,
    window_from: datetime,
    window_to: datetime,
    limit: int = _DEFAULT_RECENT_CONVERSIONS_LIMIT,
) -> RecentConversionsReport:
    """Reverse-chronological leads with ``stage = 'converted'`` and
    ``updated_at`` in ``[window_from, window_to)`` (D6).

    Verified against ``leads/admin_routes.py``'s ``_convert_lead`` /
    ``leads/repository.py``'s ``mark_lead_converted``: the SAME ``UPDATE``
    statement sets ``stage = 'converted'`` AND ``converted_to_contact_id =
    $1`` together, guarded by ``AND converted_to_contact_id IS NULL`` --
    there is no path in this codebase where one is set without the other,
    so the two candidate definitions (``stage = 'converted'`` vs
    ``converted_to_contact_id IS NOT NULL``) are equivalent here. This
    function uses ``stage = 'converted'`` because it matches the funnel's
    and leads-by-stage's existing stage-based vocabulary (D6's default).

    Deliberately does NOT inherit ``list_leads``'s ``include_converted=False``
    default (``AND converted_to_contact_id IS NULL``) -- that would silently
    return zero rows for every tenant that actually converts leads, the
    exact trap D6/M6 exists to prevent. Windows on ``updated_at`` (the
    conversion tombstone's timestamp), not ``created_at``.
    """
    _reject_global(claims)

    # Parameterized SQL; single-table over `leads`. ruff: noqa: S608
    rows = await db.fetch(
        "SELECT lead_id, name, source, stage, updated_at AS converted_at FROM leads "
        "WHERE tenant_id = $1 AND stage = 'converted' "
        "AND updated_at >= $2 AND updated_at < $3 "
        "ORDER BY updated_at DESC LIMIT $4",
        claims.tenant_id, window_from, window_to, max(1, limit),
    )

    conversions = [
        RecentConversion(
            lead_id=str(row["lead_id"]),
            name=str(row["name"]),
            source=str(row["source"]),
            stage=str(row["stage"]),
            converted_at=row["converted_at"],
        )
        for row in rows
    ]

    return RecentConversionsReport(conversions=conversions)


async def _is_currency_configured(db: Database, claims: AuthClaims) -> bool:
    """Whether the tenant has an explicit ``tenant_opportunity_configs`` row.

    A tenant with no row is silently defaulted to
    ``settings.opportunity_default_currency`` by
    ``get_opportunity_config`` -- this disclosed check is what lets the
    win/loss response distinguish "really configured USD" from "defaulted
    to USD because nobody configured anything" (SR-9.4 D7's disclosed
    risk, surfaced here rather than hidden).
    """
    row = await db.fetchrow(
        "SELECT 1 AS present FROM tenant_opportunity_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    return row is not None
