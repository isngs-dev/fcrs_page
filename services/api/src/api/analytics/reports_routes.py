"""Fixed CRM reports admin routes (SR-9.5) -- four read-only, tenant-scoped
aggregate reports (leads-by-stage, bookings, conversion funnel, win/loss),
each with a JSON GET and a CSV-export GET twin, registered on both the
implicit router and the PLATFORM_ADMIN tenant-explicit router (D1/D7),
mirroring ``analytics/routes.py``'s ``overview`` pattern exactly (M1/M10):
16 total registered handlers over 4 shared private ``_impl`` functions.

RBAC (D7): ``CLIENT_ADMIN`` + ``CLIENT_AGENT`` read every report, including
revenue/win-loss -- full symmetric read access. Zero write endpoints of any
kind. ``PLATFORM_ADMIN`` (global) is excluded from the implicit routes and
reaches tenant data only via the tenant-explicit `/admin/tenants/{tenant_id}/
analytics/reports/...` family (``resolve_tenant_scope``).

Window resolution (D4) is factored out of the shipped ``_get_overview`` into
one shared ``_resolve_window`` helper, reused by both ``overview`` and every
report here -- identical defaults/validation/error codes
(``INVALID_ANALYTICS_WINDOW``/``ANALYTICS_WINDOW_TOO_LARGE``).

CSV export (D8/D9): every cell passes through
``common.csv_safe.escape_csv_cell`` before ``csv.writer`` -- never the
vulnerable ``leads/admin_routes.py`` pattern. Each export call records an
audit row (``report_exported``, PII-free target: report name + row count
only) -- matching CLAUDE.md §3's "exports are audited" / the shipped
``overview``'s "JSON reads are not audited" precedent.

PII-safe logging (CLAUDE.md §3): log lines carry only event/tenant_id/window
bounds/bucket/report name/row counts -- never lead name/email/phone or
``close_reason``.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from common.auth import AuthClaims, Role
from common.csv_safe import escape_csv_cell
from common.errors import ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.analytics.reports_repository import (
    AgentPerformanceReport,
    AgentPerformanceRow,
    BookingsSeries,
    ConversionFunnel,
    LeadsByStage,
    LeadSourcesReport,
    RecentConversionsReport,
    ScoreDistributionReport,
    WinLossOutcome,
    WinLossReport,
    get_agent_performance,
    get_bookings_series,
    get_conversion_funnel,
    get_lead_sources,
    get_leads_by_stage,
    get_recent_conversions,
    get_score_distribution,
    get_win_loss,
)
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.config import get_api_settings

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/analytics/reports", tags=["analytics-reports"])
tenant_scoped_router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/analytics/reports", tags=["analytics-reports"]
)

_READ_ROLES = (Role.CLIENT_ADMIN, Role.CLIENT_AGENT)


# ---------------------------------------------------------------------------
# Shared window resolution (extracted from analytics/routes.py's
# `_get_overview`, D4 -- identical defaults/validation/error codes)
# ---------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC; pass through an already-aware one."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _resolve_window(
    date_from: datetime | None, date_to: datetime | None,
) -> tuple[datetime, datetime]:
    """Resolve + validate a half-open ``[from, to)`` window (D4).

    ``to`` defaults to ``now(UTC)``, ``from`` to
    ``to - analytics_default_window_days`` when omitted. Naive datetimes are
    treated as UTC. Raises ``ValidationError`` (422
    ``INVALID_ANALYTICS_WINDOW``) if ``from >= to``, and (422
    ``ANALYTICS_WINDOW_TOO_LARGE``) if the span exceeds
    ``analytics_max_window_days``.
    """
    settings = get_api_settings()

    resolved_to = _as_utc(date_to) if date_to is not None else datetime.now(tz=UTC)
    resolved_from = (
        _as_utc(date_from)
        if date_from is not None
        else resolved_to - timedelta(days=settings.analytics_default_window_days)
    )

    if resolved_from >= resolved_to:
        raise ValidationError(
            "from must be strictly before to.",
            code="INVALID_ANALYTICS_WINDOW",
        )

    if (resolved_to - resolved_from) > timedelta(days=settings.analytics_max_window_days):
        raise ValidationError(
            f"Window span may not exceed {settings.analytics_max_window_days} days.",
            code="ANALYTICS_WINDOW_TOO_LARGE",
        )

    return resolved_from, resolved_to


class ReportWindowResponse(BaseModel):
    """The resolved window, wire-compatible with ``AnalyticsWindowResponse``."""

    model_config = ConfigDict(populate_by_name=True)

    date_from: datetime = Field(alias="from")
    date_to: datetime = Field(alias="to")


def _window_payload(window_from: datetime, window_to: datetime) -> dict[str, datetime]:
    return {"from": window_from, "to": window_to}


# ---------------------------------------------------------------------------
# Report 1: leads-by-stage
# ---------------------------------------------------------------------------


class LeadsByStageResponse(BaseModel):
    window: dict[str, datetime]
    stages: dict[str, int]
    total: int


async def _leads_by_stage_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    source: str | None,
    assigned_agent_id: str | None,
) -> tuple[LeadsByStageResponse, LeadsByStage, datetime, datetime]:
    db = request.app.state.db
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_leads_by_stage(
        db, claims,
        window_from=window_from, window_to=window_to,
        source=source, assigned_agent_id=assigned_agent_id,
    )

    _log.info(
        "leads by stage report",
        extra={
            "event": "report_leads_by_stage",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "report": "leads-by-stage",
            "result_count": result.total,
        },
    )

    response = LeadsByStageResponse(
        window=_window_payload(window_from, window_to), stages=result.stages, total=result.total,
    )
    return response, result, window_from, window_to


@router.get("/leads-by-stage")
async def get_leads_by_stage_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    assigned_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> LeadsByStageResponse:
    response, _, _, _ = await _leads_by_stage_impl(
        request, claims, date_from=date_from, date_to=date_to,
        source=source, assigned_agent_id=assigned_agent_id,
    )
    return response


@tenant_scoped_router.get("/leads-by-stage")
async def get_leads_by_stage_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    assigned_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> LeadsByStageResponse:
    response, _, _, _ = await _leads_by_stage_impl(
        request, claims, date_from=date_from, date_to=date_to,
        source=source, assigned_agent_id=assigned_agent_id,
    )
    return response


_LEADS_BY_STAGE_CSV_HEADERS = ("stage", "count")


async def _leads_by_stage_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    source: str | None,
    assigned_agent_id: str | None,
) -> StreamingResponse:
    _, result, _, _ = await _leads_by_stage_impl(
        request, claims, date_from=date_from, date_to=date_to,
        source=source, assigned_agent_id=assigned_agent_id,
    )

    rows = list(result.stages.items())

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _LEADS_BY_STAGE_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for stage, count in rows:
            writer.writerow([escape_csv_cell(stage), escape_csv_cell(count)])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="leads-by-stage",
        metadata={"report": "leads-by-stage", "row_count": len(rows)},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads-by-stage.csv"},
    )


@router.get("/leads-by-stage.csv")
async def export_leads_by_stage_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    assigned_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _leads_by_stage_csv_impl(
        request, claims, date_from=date_from, date_to=date_to,
        source=source, assigned_agent_id=assigned_agent_id,
    )


@tenant_scoped_router.get("/leads-by-stage.csv")
async def export_leads_by_stage_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    assigned_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _leads_by_stage_csv_impl(
        request, claims, date_from=date_from, date_to=date_to,
        source=source, assigned_agent_id=assigned_agent_id,
    )


# ---------------------------------------------------------------------------
# Report 2: bookings
# ---------------------------------------------------------------------------

_VALID_REPORT_BUCKETS = {"day", "week", "month"}


def _validate_bucket(bucket: str) -> None:
    if bucket not in _VALID_REPORT_BUCKETS:
        raise ValidationError(
            f"bucket must be one of {sorted(_VALID_REPORT_BUCKETS)}.",
            code="INVALID_BUCKET",
        )


class BookingsBucketResponse(BaseModel):
    bucket_start: datetime
    booked: int
    completed: int
    no_show: int
    cancelled: int
    total_excluding_cancelled: int


class BookingsTotalsResponse(BaseModel):
    booked: int
    completed: int
    no_show: int
    cancelled: int
    total_excluding_cancelled: int


class BookingsReportResponse(BaseModel):
    window: dict[str, datetime | str]
    series: list[BookingsBucketResponse]
    totals: BookingsTotalsResponse


async def _bookings_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    bucket: str,
    source: str | None,
    status: str | None,
) -> tuple[BookingsReportResponse, BookingsSeries, datetime, datetime]:
    db = request.app.state.db
    _validate_bucket(bucket)
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_bookings_series(
        db, claims,
        window_from=window_from, window_to=window_to, bucket=bucket,
        source=source, status=status,
    )

    _log.info(
        "bookings report",
        extra={
            "event": "report_bookings",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "bucket": bucket,
            "report": "bookings",
            "result_count": result.totals.total_excluding_cancelled + result.totals.cancelled,
        },
    )

    window_payload: dict[str, datetime | str] = {
        "from": window_from, "to": window_to, "bucket": bucket,
    }
    response = BookingsReportResponse(
        window=window_payload,
        series=[
            BookingsBucketResponse(
                bucket_start=b.bucket_start, booked=b.booked, completed=b.completed,
                no_show=b.no_show, cancelled=b.cancelled,
                total_excluding_cancelled=b.total_excluding_cancelled,
            )
            for b in result.series
        ],
        totals=BookingsTotalsResponse(
            booked=result.totals.booked, completed=result.totals.completed,
            no_show=result.totals.no_show, cancelled=result.totals.cancelled,
            total_excluding_cancelled=result.totals.total_excluding_cancelled,
        ),
    )
    return response, result, window_from, window_to


@router.get("/bookings")
async def get_bookings_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    bucket: str = Query(default="day"),  # noqa: B008
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> BookingsReportResponse:
    response, _, _, _ = await _bookings_impl(
        request, claims, date_from=date_from, date_to=date_to,
        bucket=bucket, source=source, status=status,
    )
    return response


@tenant_scoped_router.get("/bookings")
async def get_bookings_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    bucket: str = Query(default="day"),  # noqa: B008
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> BookingsReportResponse:
    response, _, _, _ = await _bookings_impl(
        request, claims, date_from=date_from, date_to=date_to,
        bucket=bucket, source=source, status=status,
    )
    return response


_BOOKINGS_CSV_HEADERS = (
    "bucket_start", "booked", "completed", "no_show", "cancelled", "total_excluding_cancelled",
)


async def _bookings_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    bucket: str,
    source: str | None,
    status: str | None,
) -> StreamingResponse:
    _, result, _, _ = await _bookings_impl(
        request, claims, date_from=date_from, date_to=date_to,
        bucket=bucket, source=source, status=status,
    )

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _BOOKINGS_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for b in result.series:
            writer.writerow([
                escape_csv_cell(b.bucket_start.isoformat()),
                escape_csv_cell(b.booked),
                escape_csv_cell(b.completed),
                escape_csv_cell(b.no_show),
                escape_csv_cell(b.cancelled),
                escape_csv_cell(b.total_excluding_cancelled),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="bookings",
        metadata={"report": "bookings", "row_count": len(result.series)},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bookings.csv"},
    )


@router.get("/bookings.csv")
async def export_bookings_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    bucket: str = Query(default="day"),  # noqa: B008
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _bookings_csv_impl(
        request, claims, date_from=date_from, date_to=date_to,
        bucket=bucket, source=source, status=status,
    )


@tenant_scoped_router.get("/bookings.csv")
async def export_bookings_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    bucket: str = Query(default="day"),  # noqa: B008
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _bookings_csv_impl(
        request, claims, date_from=date_from, date_to=date_to,
        bucket=bucket, source=source, status=status,
    )


# ---------------------------------------------------------------------------
# Report 3: conversion funnel
# ---------------------------------------------------------------------------


class FunnelStepResponse(BaseModel):
    stage: str
    count: int
    drop_off_rate: float | None


class FunnelDisqualifiedResponse(BaseModel):
    count: int


class FunnelReportResponse(BaseModel):
    window: dict[str, datetime]
    steps: list[FunnelStepResponse]
    disqualified: FunnelDisqualifiedResponse
    overall_conversion_rate: float | None


async def _funnel_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    source: str | None,
) -> tuple[FunnelReportResponse, ConversionFunnel, datetime, datetime]:
    db = request.app.state.db
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_conversion_funnel(
        db, claims, window_from=window_from, window_to=window_to, source=source,
    )

    _log.info(
        "conversion funnel report",
        extra={
            "event": "report_funnel",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "report": "funnel",
            "result_count": sum(s.count for s in result.steps) + result.disqualified_count,
        },
    )

    response = FunnelReportResponse(
        window=_window_payload(window_from, window_to),
        steps=[
            FunnelStepResponse(stage=s.stage, count=s.count, drop_off_rate=s.drop_off_rate)
            for s in result.steps
        ],
        disqualified=FunnelDisqualifiedResponse(count=result.disqualified_count),
        overall_conversion_rate=result.overall_conversion_rate,
    )
    return response, result, window_from, window_to


@router.get("/funnel")
async def get_funnel_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> FunnelReportResponse:
    response, _, _, _ = await _funnel_impl(
        request, claims, date_from=date_from, date_to=date_to, source=source,
    )
    return response


@tenant_scoped_router.get("/funnel")
async def get_funnel_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> FunnelReportResponse:
    response, _, _, _ = await _funnel_impl(
        request, claims, date_from=date_from, date_to=date_to, source=source,
    )
    return response


_FUNNEL_CSV_HEADERS = ("stage", "count", "drop_off_rate")


async def _funnel_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    source: str | None,
) -> StreamingResponse:
    _, result, _, _ = await _funnel_impl(
        request, claims, date_from=date_from, date_to=date_to, source=source,
    )

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _FUNNEL_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for step in result.steps:
            writer.writerow([
                escape_csv_cell(step.stage),
                escape_csv_cell(step.count),
                escape_csv_cell(step.drop_off_rate),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
        writer.writerow([
            escape_csv_cell("disqualified"),
            escape_csv_cell(result.disqualified_count),
            escape_csv_cell(""),
        ])
        yield buffer.getvalue()

    row_count = len(result.steps) + 1
    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="funnel",
        metadata={"report": "funnel", "row_count": row_count},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=funnel.csv"},
    )


@router.get("/funnel.csv")
async def export_funnel_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _funnel_csv_impl(
        request, claims, date_from=date_from, date_to=date_to, source=source,
    )


@tenant_scoped_router.get("/funnel.csv")
async def export_funnel_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    source: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _funnel_csv_impl(
        request, claims, date_from=date_from, date_to=date_to, source=source,
    )


# ---------------------------------------------------------------------------
# Report 4: win/loss
# ---------------------------------------------------------------------------


class WinLossOutcomeResponse(BaseModel):
    count: int
    amount_total: Decimal
    amount_null_count: int
    avg_deal_size: Decimal | None
    avg_days_to_close: float | None


class LossReasonResponse(BaseModel):
    closed_at: datetime
    close_reason: str | None


class WinLossReportResponse(BaseModel):
    window: dict[str, datetime]
    currency: str
    currency_configured: bool
    won: WinLossOutcomeResponse
    lost: WinLossOutcomeResponse
    win_rate: float | None
    loss_reasons: list[LossReasonResponse]


def _outcome_response(outcome: WinLossOutcome) -> WinLossOutcomeResponse:
    return WinLossOutcomeResponse(
        count=outcome.count,
        amount_total=outcome.amount_total,
        amount_null_count=outcome.amount_null_count,
        avg_deal_size=outcome.avg_deal_size,
        avg_days_to_close=outcome.avg_days_to_close,
    )


async def _win_loss_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    owner_agent_id: str | None,
) -> tuple[WinLossReportResponse, WinLossReport, datetime, datetime]:
    db = request.app.state.db
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_win_loss(
        db, claims, window_from=window_from, window_to=window_to, owner_agent_id=owner_agent_id,
    )

    _log.info(
        "win loss report",
        extra={
            "event": "report_win_loss",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "report": "win-loss",
            "result_count": result.won.count + result.lost.count,
        },
    )

    response = WinLossReportResponse(
        window=_window_payload(window_from, window_to),
        currency=result.currency,
        currency_configured=result.currency_configured,
        won=_outcome_response(result.won),
        lost=_outcome_response(result.lost),
        win_rate=result.win_rate,
        loss_reasons=[
            LossReasonResponse(closed_at=r.closed_at, close_reason=r.close_reason)
            for r in result.loss_reasons
        ],
    )
    return response, result, window_from, window_to


@router.get("/win-loss")
async def get_win_loss_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    owner_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> WinLossReportResponse:
    response, _, _, _ = await _win_loss_impl(
        request, claims, date_from=date_from, date_to=date_to, owner_agent_id=owner_agent_id,
    )
    return response


@tenant_scoped_router.get("/win-loss")
async def get_win_loss_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    owner_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> WinLossReportResponse:
    response, _, _, _ = await _win_loss_impl(
        request, claims, date_from=date_from, date_to=date_to, owner_agent_id=owner_agent_id,
    )
    return response


_WIN_LOSS_CSV_HEADERS = ("outcome", "closed_at", "close_reason")


async def _win_loss_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    owner_agent_id: str | None,
) -> StreamingResponse:
    _, result, _, _ = await _win_loss_impl(
        request, claims, date_from=date_from, date_to=date_to, owner_agent_id=owner_agent_id,
    )

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _WIN_LOSS_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for reason in result.loss_reasons:
            writer.writerow([
                escape_csv_cell("closed_lost"),
                escape_csv_cell(reason.closed_at.isoformat()),
                escape_csv_cell(reason.close_reason),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    row_count = len(result.loss_reasons)
    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="win-loss",
        metadata={"report": "win-loss", "row_count": row_count},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=win-loss.csv"},
    )


@router.get("/win-loss.csv")
async def export_win_loss_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    owner_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _win_loss_csv_impl(
        request, claims, date_from=date_from, date_to=date_to, owner_agent_id=owner_agent_id,
    )


@tenant_scoped_router.get("/win-loss.csv")
async def export_win_loss_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    owner_agent_id: str | None = Query(default=None),
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _win_loss_csv_impl(
        request, claims, date_from=date_from, date_to=date_to, owner_agent_id=owner_agent_id,
    )


# ---------------------------------------------------------------------------
# SR-19: four new read-only aggregates over `leads` -- lead sources, score
# distribution, agent performance, recent conversions. Same shape as the
# four reports above: paired routers, one shared `_impl`, `_resolve_window`,
# `escape_csv_cell`, `report_exported` audit, PII-safe logging (D1/D3).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Report 5: lead sources (D4)
# ---------------------------------------------------------------------------


class LeadSourceResponse(BaseModel):
    source: str
    count: int
    percentage: float


class LeadSourcesResponse(BaseModel):
    window: dict[str, datetime]
    sources: list[LeadSourceResponse]
    total: int
    single_source: bool


async def _lead_sources_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[LeadSourcesResponse, LeadSourcesReport, datetime, datetime]:
    db = request.app.state.db
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_lead_sources(db, claims, window_from=window_from, window_to=window_to)

    _log.info(
        "lead sources report",
        extra={
            "event": "report_lead_sources",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "report": "lead-sources",
            "result_count": result.total,
        },
    )

    response = LeadSourcesResponse(
        window=_window_payload(window_from, window_to),
        sources=[
            LeadSourceResponse(source=s.source, count=s.count, percentage=s.percentage)
            for s in result.sources
        ],
        total=result.total,
        single_source=result.single_source,
    )
    return response, result, window_from, window_to


@router.get("/lead-sources")
async def get_lead_sources_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> LeadSourcesResponse:
    response, _, _, _ = await _lead_sources_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )
    return response


@tenant_scoped_router.get("/lead-sources")
async def get_lead_sources_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> LeadSourcesResponse:
    response, _, _, _ = await _lead_sources_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )
    return response


_LEAD_SOURCES_CSV_HEADERS = ("source", "count", "percentage")


async def _lead_sources_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> StreamingResponse:
    _, result, _, _ = await _lead_sources_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _LEAD_SOURCES_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for s in result.sources:
            writer.writerow([
                escape_csv_cell(s.source), escape_csv_cell(s.count), escape_csv_cell(s.percentage),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="lead-sources",
        metadata={"report": "lead-sources", "row_count": len(result.sources)},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lead-sources.csv"},
    )


@router.get("/lead-sources.csv")
async def export_lead_sources_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _lead_sources_csv_impl(request, claims, date_from=date_from, date_to=date_to)


@tenant_scoped_router.get("/lead-sources.csv")
async def export_lead_sources_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _lead_sources_csv_impl(request, claims, date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# Report 6: score distribution (D8)
# ---------------------------------------------------------------------------


class ScoreDistributionResponse(BaseModel):
    window: dict[str, datetime]
    bands: dict[str, int]
    unscored: int
    total: int


async def _score_distribution_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[ScoreDistributionResponse, ScoreDistributionReport, datetime, datetime]:
    db = request.app.state.db
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_score_distribution(db, claims, window_from=window_from, window_to=window_to)

    _log.info(
        "score distribution report",
        extra={
            "event": "report_score_distribution",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "report": "score-distribution",
            "result_count": result.total,
        },
    )

    response = ScoreDistributionResponse(
        window=_window_payload(window_from, window_to),
        bands=result.bands,
        unscored=result.unscored,
        total=result.total,
    )
    return response, result, window_from, window_to


@router.get("/score-distribution")
async def get_score_distribution_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> ScoreDistributionResponse:
    response, _, _, _ = await _score_distribution_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )
    return response


@tenant_scoped_router.get("/score-distribution")
async def get_score_distribution_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> ScoreDistributionResponse:
    response, _, _, _ = await _score_distribution_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )
    return response


_SCORE_DISTRIBUTION_CSV_HEADERS = ("band", "count")


async def _score_distribution_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> StreamingResponse:
    _, result, _, _ = await _score_distribution_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )

    rows = [*result.bands.items(), ("unscored", result.unscored)]

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _SCORE_DISTRIBUTION_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for band, count in rows:
            writer.writerow([escape_csv_cell(band), escape_csv_cell(count)])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="score-distribution",
        metadata={"report": "score-distribution", "row_count": len(rows)},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=score-distribution.csv"},
    )


@router.get("/score-distribution.csv")
async def export_score_distribution_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _score_distribution_csv_impl(request, claims, date_from=date_from, date_to=date_to)


@tenant_scoped_router.get("/score-distribution.csv")
async def export_score_distribution_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _score_distribution_csv_impl(request, claims, date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# Report 7: agent performance (D7)
# ---------------------------------------------------------------------------


class AgentPerformanceRowResponse(BaseModel):
    assigned_agent_id: str | None
    assigned: int
    contacted: int
    won: int
    win_rate: float | None


class AgentPerformanceResponse(BaseModel):
    window: dict[str, datetime]
    agents: list[AgentPerformanceRowResponse]
    unassigned: AgentPerformanceRowResponse


def _agent_row_response(row: AgentPerformanceRow) -> AgentPerformanceRowResponse:
    return AgentPerformanceRowResponse(
        assigned_agent_id=row.assigned_agent_id,
        assigned=row.assigned, contacted=row.contacted, won=row.won, win_rate=row.win_rate,
    )


async def _agent_performance_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[AgentPerformanceResponse, AgentPerformanceReport, datetime, datetime]:
    db = request.app.state.db
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_agent_performance(db, claims, window_from=window_from, window_to=window_to)

    # PII-safe: NEVER log assigned_agent_id (D3) -- row counts only.
    _log.info(
        "agent performance report",
        extra={
            "event": "report_agent_performance",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "report": "agent-performance",
            "result_count": len(result.agents) + 1,
        },
    )

    response = AgentPerformanceResponse(
        window=_window_payload(window_from, window_to),
        agents=[_agent_row_response(a) for a in result.agents],
        unassigned=_agent_row_response(result.unassigned),
    )
    return response, result, window_from, window_to


@router.get("/agent-performance")
async def get_agent_performance_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> AgentPerformanceResponse:
    response, _, _, _ = await _agent_performance_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )
    return response


@tenant_scoped_router.get("/agent-performance")
async def get_agent_performance_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> AgentPerformanceResponse:
    response, _, _, _ = await _agent_performance_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )
    return response


_AGENT_PERFORMANCE_CSV_HEADERS = ("assigned_agent_id", "assigned", "contacted", "won", "win_rate")


async def _agent_performance_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> StreamingResponse:
    _, result, _, _ = await _agent_performance_impl(
        request, claims, date_from=date_from, date_to=date_to,
    )

    rows = [*result.agents, result.unassigned]

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _AGENT_PERFORMANCE_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for a in rows:
            agent_label = a.assigned_agent_id if a.assigned_agent_id is not None else "Unassigned"
            writer.writerow([
                escape_csv_cell(agent_label),
                escape_csv_cell(a.assigned),
                escape_csv_cell(a.contacted),
                escape_csv_cell(a.won),
                escape_csv_cell(a.win_rate),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    # PII-safe audit target: report name + row count only -- NEVER an
    # assigned_agent_id (D3).
    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="agent-performance",
        metadata={"report": "agent-performance", "row_count": len(rows)},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=agent-performance.csv"},
    )


@router.get("/agent-performance.csv")
async def export_agent_performance_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _agent_performance_csv_impl(request, claims, date_from=date_from, date_to=date_to)


@tenant_scoped_router.get("/agent-performance.csv")
async def export_agent_performance_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _agent_performance_csv_impl(request, claims, date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# Report 8: recent conversions (D6)
# ---------------------------------------------------------------------------

_DEFAULT_RECENT_CONVERSIONS_LIMIT = 50


class RecentConversionResponse(BaseModel):
    lead_id: str
    name: str
    source: str
    stage: str
    converted_at: datetime


class RecentConversionsResponse(BaseModel):
    window: dict[str, datetime]
    conversions: list[RecentConversionResponse]


async def _recent_conversions_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
) -> tuple[RecentConversionsResponse, RecentConversionsReport, datetime, datetime]:
    db = request.app.state.db
    window_from, window_to = _resolve_window(date_from, date_to)

    result = await get_recent_conversions(
        db, claims, window_from=window_from, window_to=window_to, limit=limit,
    )

    # PII-safe: NEVER log a lead name/email/phone (D3) -- row count only.
    _log.info(
        "recent conversions report",
        extra={
            "event": "report_recent_conversions",
            "tenant_id": claims.tenant_id,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "report": "recent-conversions",
            "result_count": len(result.conversions),
        },
    )

    response = RecentConversionsResponse(
        window=_window_payload(window_from, window_to),
        conversions=[
            RecentConversionResponse(
                lead_id=c.lead_id, name=c.name, source=c.source,
                stage=c.stage, converted_at=c.converted_at,
            )
            for c in result.conversions
        ],
    )
    return response, result, window_from, window_to


@router.get("/recent-conversions")
async def get_recent_conversions_route(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    limit: int = Query(default=_DEFAULT_RECENT_CONVERSIONS_LIMIT),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> RecentConversionsResponse:
    response, _, _, _ = await _recent_conversions_impl(
        request, claims, date_from=date_from, date_to=date_to, limit=limit,
    )
    return response


@tenant_scoped_router.get("/recent-conversions")
async def get_recent_conversions_route_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    limit: int = Query(default=_DEFAULT_RECENT_CONVERSIONS_LIMIT),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> RecentConversionsResponse:
    response, _, _, _ = await _recent_conversions_impl(
        request, claims, date_from=date_from, date_to=date_to, limit=limit,
    )
    return response


_RECENT_CONVERSIONS_CSV_HEADERS = ("lead_id", "name", "source", "stage", "converted_at")


async def _recent_conversions_csv_impl(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
) -> StreamingResponse:
    _, result, _, _ = await _recent_conversions_impl(
        request, claims, date_from=date_from, date_to=date_to, limit=limit,
    )

    def _generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([escape_csv_cell(h) for h in _RECENT_CONVERSIONS_CSV_HEADERS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for c in result.conversions:
            writer.writerow([
                escape_csv_cell(c.lead_id),
                escape_csv_cell(c.name),
                escape_csv_cell(c.source),
                escape_csv_cell(c.stage),
                escape_csv_cell(c.converted_at.isoformat()),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    # PII-safe audit target: report name + row count only -- NEVER a lead
    # name/email/phone (D3).
    await record_audit(
        request.app.state.db,
        claims,
        action="report_exported",
        target_type="report",
        target_id="recent-conversions",
        metadata={"report": "recent-conversions", "row_count": len(result.conversions)},
        actor_context=get_platform_admin_actor(request),
    )

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=recent-conversions.csv"},
    )


@router.get("/recent-conversions.csv")
async def export_recent_conversions_csv(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    limit: int = Query(default=_DEFAULT_RECENT_CONVERSIONS_LIMIT),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _recent_conversions_csv_impl(
        request, claims, date_from=date_from, date_to=date_to, limit=limit,
    )


@tenant_scoped_router.get("/recent-conversions.csv")
async def export_recent_conversions_csv_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    limit: int = Query(default=_DEFAULT_RECENT_CONVERSIONS_LIMIT),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(*_READ_ROLES)),  # noqa: B008
) -> StreamingResponse:
    return await _recent_conversions_csv_impl(
        request, claims, date_from=date_from, date_to=date_to, limit=limit,
    )
