"""Admin conversation-analytics routes -- GET /admin/analytics/overview (S11.2).

A single, tenant-scoped, read-only admin surface reporting how the bot is
performing over a caller-chosen time window: fallback rate, deflection rate,
grounded rate, intent/decision distribution, schedule conversion, and a
time-bucketed series. ``CLIENT_ADMIN`` + ``CLIENT_AGENT`` only -- a
``VISITOR``/unauthenticated caller is rejected by ``require_roles`` before
this handler runs, and a ``PLATFORM_ADMIN`` (global) is likewise not in the
allowed role set (the repository's ``_reject_global`` is defense-in-depth).

Leak-free response (CLAUDE.md §3 / S11.2 decision 9): only aggregate counts,
rounded rates, ISO bucket timestamps, and closed-set label keys -- never
``tenant_id``/``visitor_id``/``conversation_id``/``message_id``/message text.
The log line is likewise PII-free (window bounds + bucket + tenant_id,
server-side only -- no row content).
"""
from __future__ import annotations

from datetime import datetime

from common.auth import AuthClaims, Role
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.analytics.reports_routes import _resolve_window
from api.analytics.repository import AnalyticsOverview, get_analytics_overview
from api.auth.dependencies import require_roles, resolve_tenant_scope

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])
tenant_scoped_router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/analytics", tags=["analytics"]
)


class AnalyticsBucketResponse(BaseModel):
    """One time-bucketed slice of the series."""

    bucket_start: datetime
    conversations: int
    answers: int
    escalations: int
    bookings: int


class AnalyticsWindowResponse(BaseModel):
    """The resolved (possibly defaulted) window + bucket granularity.

    Serializes as ``{"from": ..., "to": ..., "bucket": ...}`` (Field aliases)
    -- ``from`` is a Python keyword, so the model attribute is ``date_from``/
    ``date_to`` but the wire shape matches the spec exactly.
    """

    model_config = ConfigDict(populate_by_name=True)

    date_from: datetime = Field(alias="from")
    date_to: datetime = Field(alias="to")
    bucket: str


class AnalyticsTotalsResponse(BaseModel):
    """Raw counts backing the rates -- nothing is lost to rounding."""

    conversations: int
    user_turns: int
    bot_turns: int
    decided_bot_turns: int


class AnalyticsScheduleResponse(BaseModel):
    """Schedule-conversion counts + rate (visitor_id-correlated approximation)."""

    cta_conversations: int
    conversions: int
    conversion_rate: float | None


class AnalyticsOverviewResponse(BaseModel):
    """The full nested response shape for GET /admin/analytics/overview."""

    window: AnalyticsWindowResponse
    totals: AnalyticsTotalsResponse
    intent_distribution: dict[str, int]
    decision_distribution: dict[str, int]
    fallback_rate: float | None
    deflection_rate: float | None
    grounded_rate: float | None
    schedule: AnalyticsScheduleResponse
    series: list[AnalyticsBucketResponse]


def _to_response(overview: AnalyticsOverview) -> AnalyticsOverviewResponse:
    return AnalyticsOverviewResponse(
        window=AnalyticsWindowResponse.model_validate(
            {
                "from": overview.window_from,
                "to": overview.window_to,
                "bucket": overview.bucket,
            }
        ),
        totals=AnalyticsTotalsResponse(
            conversations=overview.total_conversations,
            user_turns=overview.total_user_turns,
            bot_turns=overview.total_bot_turns,
            decided_bot_turns=overview.decided_bot_turns,
        ),
        intent_distribution=overview.intent_distribution,
        decision_distribution=overview.decision_distribution,
        fallback_rate=overview.fallback_rate,
        deflection_rate=overview.deflection_rate,
        grounded_rate=overview.grounded_rate,
        schedule=AnalyticsScheduleResponse(
            cta_conversations=overview.schedule_cta_conversations,
            conversions=overview.schedule_conversions,
            conversion_rate=overview.schedule_conversion_rate,
        ),
        series=[
            AnalyticsBucketResponse(
                bucket_start=b.bucket_start,
                conversations=b.conversations,
                answers=b.answers,
                escalations=b.escalations,
                bookings=b.bookings,
            )
            for b in overview.series
        ],
    )


async def _get_overview(
    request: Request,
    claims: AuthClaims,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    bucket: str,
) -> AnalyticsOverviewResponse:
    """Return the tenant-scoped conversation-analytics overview.

    Defaults: ``to = now(UTC)``, ``from = to - analytics_default_window_days``
    when omitted. Naive datetimes are treated as UTC. Validates ``from < to``
    (422 ``INVALID_ANALYTICS_WINDOW``) and the span against
    ``analytics_max_window_days`` (422 ``ANALYTICS_WINDOW_TOO_LARGE``);
    ``bucket`` validation is delegated to the repository (422
    ``INVALID_BUCKET``).
    """
    db = request.app.state.db

    resolved_from, resolved_to = _resolve_window(date_from, date_to)

    overview = await get_analytics_overview(
        db,
        claims,
        window_from=resolved_from,
        window_to=resolved_to,
        bucket=bucket,
    )

    _log.info(
        "analytics overview",
        extra={
            "event": "analytics_overview",
            "tenant_id": claims.tenant_id,
            "window_from": resolved_from.isoformat(),
            "window_to": resolved_to.isoformat(),
            "bucket": bucket,
            "total_conversations": overview.total_conversations,
        },
    )

    return _to_response(overview)


@router.get("/overview")
async def get_overview(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    bucket: str = Query(default="day"),  # noqa: B008
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AnalyticsOverviewResponse:
    return await _get_overview(request, claims, date_from=date_from, date_to=date_to, bucket=bucket)


@tenant_scoped_router.get("/overview")
async def get_overview_for_tenant(
    request: Request,
    date_from: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    date_to: datetime | None = Query(default=None, alias="to"),  # noqa: B008
    bucket: str = Query(default="day"),  # noqa: B008
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> AnalyticsOverviewResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/analytics/overview`` (S12.7)."""
    return await _get_overview(request, claims, date_from=date_from, date_to=date_to, bucket=bucket)
