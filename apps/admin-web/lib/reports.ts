/**
 * Server-only data layer for the fixed CRM reports console (SR-9.5). Mirrors
 * `lib/analytics.ts`'s shape exactly: a pure, unit-testable window resolver,
 * one fetch function per report mapping the backend's response (or any
 * error) into a discriminated `{status:"ok"|"error"}` result -- no silent
 * fallbacks (CLAUDE.md §3): a backend error always becomes a visible,
 * honest state, and a `null` rate is NEVER coerced to `0`.
 *
 * Constants/shapes below are sourced verbatim from the real backend:
 *  - `REPORT_WINDOW_RANGES` mirrors the shared 30-day default / 366-day cap
 *    (services/api/src/api/config.py `analytics_default_window_days`/
 *    `analytics_max_window_days`) -- identical to `lib/analytics.ts`'s
 *    `ANALYTICS_RANGES`.
 *  - `REPORT_BUCKETS` mirrors `analytics/repository.py`'s `_VALID_BUCKETS`
 *    (`{day, week, month}`, SR-9.5 D3) -- reused from `lib/analytics.ts`'s
 *    `ANALYTICS_BUCKETS` so the two screens never disagree on vocabulary.
 *  - Response shapes mirror `analytics/reports_routes.py`'s Pydantic models
 *    exactly (`LeadsByStageResponse`, `BookingsReportResponse`,
 *    `FunnelReportResponse`, `WinLossReportResponse`).
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";
import { ANALYTICS_BUCKETS, ANALYTICS_RANGES, DEFAULT_RANGE_KEY, type AnalyticsBucket } from "@/lib/analytics";

export const REPORT_BUCKETS = ANALYTICS_BUCKETS;
export type ReportBucket = AnalyticsBucket;
export const DEFAULT_REPORT_BUCKET: ReportBucket = "day";

export interface ReportQueryParams {
  range?: string;
  bucket?: string;
  source?: string;
  status?: string;
  assignedAgentId?: string;
  ownerAgentId?: string;
}

/**
 * Pure, unit-testable window resolver (mirrors `resolveAnalyticsQuery`).
 * An unknown/blank `range` -> default `30d`. `to = now(UTC)`,
 * `from = to - days`. Extra filters (`source`/`status`/`assignedAgentId`/
 * `ownerAgentId`) are passed through verbatim when present.
 */
export function resolveReportQuery(params: ReportQueryParams): string {
  const rangeKey = params.range?.trim();
  const range =
    ANALYTICS_RANGES.find((r) => r.key === rangeKey) ??
    ANALYTICS_RANGES.find((r) => r.key === DEFAULT_RANGE_KEY)!;
  const to = new Date();
  const from = new Date(to.getTime() - range.days * 24 * 60 * 60 * 1000);

  const query = new URLSearchParams();
  query.set("from", from.toISOString());
  query.set("to", to.toISOString());

  if (params.source) query.set("source", params.source);
  if (params.status) query.set("status", params.status);
  if (params.assignedAgentId) query.set("assigned_agent_id", params.assignedAgentId);
  if (params.ownerAgentId) query.set("owner_agent_id", params.ownerAgentId);

  return query.toString();
}

/** `resolveReportQuery` + a validated `bucket` (bookings report only). */
export function resolveBookingsQuery(params: ReportQueryParams): string {
  const base = resolveReportQuery(params);
  const bucketValue = params.bucket?.trim();
  const bucket =
    bucketValue && (REPORT_BUCKETS as readonly string[]).includes(bucketValue)
      ? (bucketValue as ReportBucket)
      : DEFAULT_REPORT_BUCKET;
  const query = new URLSearchParams(base);
  query.set("bucket", bucket);
  return query.toString();
}

export type ReportResult<T> =
  | { status: "ok"; data: T }
  | { status: "error"; message: string; correlationId: string };

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this report.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  if (error.status === 404 || error.errorCode === "TENANT_NOT_FOUND") {
    return "That tenant could not be found.";
  }
  if (
    error.errorCode === "INVALID_ANALYTICS_WINDOW" ||
    error.errorCode === "ANALYTICS_WINDOW_TOO_LARGE" ||
    error.errorCode === "INVALID_BUCKET"
  ) {
    return "That date range or bucket isn't valid -- showing the default window.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

async function fetchReport<TBody, TData>(
  path: string,
  toData: (body: TBody) => TData
): Promise<ReportResult<TData>> {
  try {
    const response = await adminApiFetch(path);
    const body = (await response.json()) as TBody;
    return { status: "ok", data: toData(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

function reportBasePath(report: string, tenantId?: string): string {
  return tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/analytics/reports/${report}`
    : `/admin/analytics/reports/${report}`;
}

// ---------------------------------------------------------------------------
// Leads by stage
// ---------------------------------------------------------------------------

export interface LeadsByStageReport {
  window: { from: string; to: string };
  stages: Record<string, number>;
  total: number;
}

interface LeadsByStageResponseBody {
  window: { from: string; to: string };
  stages: Record<string, number>;
  total: number;
}

function toLeadsByStageReport(body: LeadsByStageResponseBody): LeadsByStageReport {
  return { window: body.window, stages: body.stages, total: body.total };
}

export async function getLeadsByStageReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<LeadsByStageReport>> {
  const query = resolveReportQuery(params);
  return fetchReport<LeadsByStageResponseBody, LeadsByStageReport>(
    `${reportBasePath("leads-by-stage", tenantId)}?${query}`,
    toLeadsByStageReport
  );
}

// ---------------------------------------------------------------------------
// Bookings
// ---------------------------------------------------------------------------

export interface BookingsBucketPoint {
  bucketStart: string;
  booked: number;
  completed: number;
  noShow: number;
  cancelled: number;
  totalExcludingCancelled: number;
}

export interface BookingsReport {
  window: { from: string; to: string; bucket: string };
  series: BookingsBucketPoint[];
  totals: Omit<BookingsBucketPoint, "bucketStart">;
}

interface BookingsBucketResponseBody {
  bucket_start: string;
  booked: number;
  completed: number;
  no_show: number;
  cancelled: number;
  total_excluding_cancelled: number;
}

interface BookingsResponseBody {
  window: { from: string; to: string; bucket: string };
  series: BookingsBucketResponseBody[];
  totals: Omit<BookingsBucketResponseBody, "bucket_start">;
}

function toBookingsBucket(b: BookingsBucketResponseBody): BookingsBucketPoint {
  return {
    bucketStart: b.bucket_start,
    booked: b.booked,
    completed: b.completed,
    noShow: b.no_show,
    cancelled: b.cancelled,
    totalExcludingCancelled: b.total_excluding_cancelled,
  };
}

function toBookingsReport(body: BookingsResponseBody): BookingsReport {
  return {
    window: body.window,
    series: body.series.map(toBookingsBucket),
    totals: {
      booked: body.totals.booked,
      completed: body.totals.completed,
      noShow: body.totals.no_show,
      cancelled: body.totals.cancelled,
      totalExcludingCancelled: body.totals.total_excluding_cancelled,
    },
  };
}

export async function getBookingsReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<BookingsReport>> {
  const query = resolveBookingsQuery(params);
  return fetchReport<BookingsResponseBody, BookingsReport>(
    `${reportBasePath("bookings", tenantId)}?${query}`,
    toBookingsReport
  );
}

// ---------------------------------------------------------------------------
// Conversion funnel
// ---------------------------------------------------------------------------

export interface FunnelStep {
  stage: string;
  count: number;
  dropOffRate: number | null;
}

export interface FunnelReport {
  window: { from: string; to: string };
  steps: FunnelStep[];
  disqualifiedCount: number;
  overallConversionRate: number | null;
}

interface FunnelResponseBody {
  window: { from: string; to: string };
  steps: { stage: string; count: number; drop_off_rate: number | null }[];
  disqualified: { count: number };
  overall_conversion_rate: number | null;
}

function toFunnelReport(body: FunnelResponseBody): FunnelReport {
  return {
    window: body.window,
    steps: body.steps.map((s) => ({ stage: s.stage, count: s.count, dropOffRate: s.drop_off_rate })),
    disqualifiedCount: body.disqualified.count,
    // `null` preserved as `null` -- never coerced to `0` (D12).
    overallConversionRate: body.overall_conversion_rate,
  };
}

export async function getFunnelReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<FunnelReport>> {
  const query = resolveReportQuery(params);
  return fetchReport<FunnelResponseBody, FunnelReport>(
    `${reportBasePath("funnel", tenantId)}?${query}`,
    toFunnelReport
  );
}

// ---------------------------------------------------------------------------
// Win / loss
// ---------------------------------------------------------------------------

export interface WinLossOutcome {
  count: number;
  amountTotal: string;
  amountNullCount: number;
  avgDealSize: string | null;
  avgDaysToClose: number | null;
}

export interface LossReason {
  closedAt: string;
  closeReason: string | null;
}

export interface WinLossReport {
  window: { from: string; to: string };
  currency: string;
  currencyConfigured: boolean;
  won: WinLossOutcome;
  lost: WinLossOutcome;
  winRate: number | null;
  lossReasons: LossReason[];
}

interface WinLossOutcomeResponseBody {
  count: number;
  amount_total: string;
  amount_null_count: number;
  avg_deal_size: string | null;
  avg_days_to_close: number | null;
}

interface WinLossResponseBody {
  window: { from: string; to: string };
  currency: string;
  currency_configured: boolean;
  won: WinLossOutcomeResponseBody;
  lost: WinLossOutcomeResponseBody;
  win_rate: number | null;
  loss_reasons: { closed_at: string; close_reason: string | null }[];
}

function toOutcome(o: WinLossOutcomeResponseBody): WinLossOutcome {
  return {
    count: o.count,
    amountTotal: o.amount_total,
    amountNullCount: o.amount_null_count,
    avgDealSize: o.avg_deal_size,
    avgDaysToClose: o.avg_days_to_close,
  };
}

function toWinLossReport(body: WinLossResponseBody): WinLossReport {
  return {
    window: body.window,
    currency: body.currency,
    currencyConfigured: body.currency_configured,
    won: toOutcome(body.won),
    lost: toOutcome(body.lost),
    winRate: body.win_rate,
    lossReasons: body.loss_reasons.map((r) => ({ closedAt: r.closed_at, closeReason: r.close_reason })),
  };
}

export async function getWinLossReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<WinLossReport>> {
  const query = resolveReportQuery(params);
  return fetchReport<WinLossResponseBody, WinLossReport>(
    `${reportBasePath("win-loss", tenantId)}?${query}`,
    toWinLossReport
  );
}

// ---------------------------------------------------------------------------
// Lead sources (SR-19 D4)
// ---------------------------------------------------------------------------

export interface LeadSource {
  source: string;
  count: number;
  percentage: number;
}

export interface LeadSourcesReport {
  window: { from: string; to: string };
  sources: LeadSource[];
  total: number;
  /** D4 -- true when exactly one real source exists in the window. The UI
   * uses this to show an honest note instead of a padded/broken-looking
   * donut. A real tenant is expected to be single-source for a long time
   * (`leads.source` defaults to `'widget'`, currently the only channel) --
   * this is NOT a bug and must never be "fixed" by padding fake categories. */
  singleSource: boolean;
}

interface LeadSourcesResponseBody {
  window: { from: string; to: string };
  sources: LeadSource[];
  total: number;
  single_source: boolean;
}

function toLeadSourcesReport(body: LeadSourcesResponseBody): LeadSourcesReport {
  return { window: body.window, sources: body.sources, total: body.total, singleSource: body.single_source };
}

export async function getLeadSourcesReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<LeadSourcesReport>> {
  const query = resolveReportQuery(params);
  return fetchReport<LeadSourcesResponseBody, LeadSourcesReport>(
    `${reportBasePath("lead-sources", tenantId)}?${query}`,
    toLeadSourcesReport
  );
}

// ---------------------------------------------------------------------------
// Score distribution (SR-19 D8)
// ---------------------------------------------------------------------------

export interface ScoreDistributionReport {
  window: { from: string; to: string };
  bands: Record<string, number>;
  unscored: number;
  total: number;
}

interface ScoreDistributionResponseBody {
  window: { from: string; to: string };
  bands: Record<string, number>;
  unscored: number;
  total: number;
}

function toScoreDistributionReport(body: ScoreDistributionResponseBody): ScoreDistributionReport {
  return { window: body.window, bands: body.bands, unscored: body.unscored, total: body.total };
}

export async function getScoreDistributionReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<ScoreDistributionReport>> {
  const query = resolveReportQuery(params);
  return fetchReport<ScoreDistributionResponseBody, ScoreDistributionReport>(
    `${reportBasePath("score-distribution", tenantId)}?${query}`,
    toScoreDistributionReport
  );
}

// ---------------------------------------------------------------------------
// Agent performance (SR-19 D7)
// ---------------------------------------------------------------------------

export interface AgentPerformanceRow {
  assignedAgentId: string | null;
  assigned: number;
  contacted: number;
  won: number;
  /** null (never 0) when `contacted` is 0 -- render as "No data" (D7). */
  winRate: number | null;
}

export interface AgentPerformanceReport {
  window: { from: string; to: string };
  agents: AgentPerformanceRow[];
  /** Leads with no assigned_agent_id -- always present, never dropped, so
   * the table's counts sum to the tenant's total in the window (D7). */
  unassigned: AgentPerformanceRow;
}

interface AgentPerformanceRowResponseBody {
  assigned_agent_id: string | null;
  assigned: number;
  contacted: number;
  won: number;
  win_rate: number | null;
}

interface AgentPerformanceResponseBody {
  window: { from: string; to: string };
  agents: AgentPerformanceRowResponseBody[];
  unassigned: AgentPerformanceRowResponseBody;
}

function toAgentRow(row: AgentPerformanceRowResponseBody): AgentPerformanceRow {
  return {
    assignedAgentId: row.assigned_agent_id,
    assigned: row.assigned,
    contacted: row.contacted,
    won: row.won,
    // `null` preserved as `null` -- never coerced to `0` (D7/D12).
    winRate: row.win_rate,
  };
}

function toAgentPerformanceReport(body: AgentPerformanceResponseBody): AgentPerformanceReport {
  return {
    window: body.window,
    agents: body.agents.map(toAgentRow),
    unassigned: toAgentRow(body.unassigned),
  };
}

export async function getAgentPerformanceReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<AgentPerformanceReport>> {
  const query = resolveReportQuery(params);
  return fetchReport<AgentPerformanceResponseBody, AgentPerformanceReport>(
    `${reportBasePath("agent-performance", tenantId)}?${query}`,
    toAgentPerformanceReport
  );
}

// ---------------------------------------------------------------------------
// Recent conversions (SR-19 D6 -- deliberately NO value field, see lib
// header + backend docstring for why)
// ---------------------------------------------------------------------------

export interface RecentConversion {
  leadId: string;
  name: string;
  source: string;
  stage: string;
  convertedAt: string;
}

export interface RecentConversionsReport {
  window: { from: string; to: string };
  conversions: RecentConversion[];
}

interface RecentConversionResponseBody {
  lead_id: string;
  name: string;
  source: string;
  stage: string;
  converted_at: string;
}

interface RecentConversionsResponseBody {
  window: { from: string; to: string };
  conversions: RecentConversionResponseBody[];
}

function toRecentConversionsReport(body: RecentConversionsResponseBody): RecentConversionsReport {
  return {
    window: body.window,
    conversions: body.conversions.map((c) => ({
      leadId: c.lead_id,
      name: c.name,
      source: c.source,
      stage: c.stage,
      convertedAt: c.converted_at,
    })),
  };
}

export async function getRecentConversionsReport(
  params: ReportQueryParams,
  tenantId?: string
): Promise<ReportResult<RecentConversionsReport>> {
  const query = resolveReportQuery(params);
  return fetchReport<RecentConversionsResponseBody, RecentConversionsReport>(
    `${reportBasePath("recent-conversions", tenantId)}?${query}`,
    toRecentConversionsReport
  );
}

// ---------------------------------------------------------------------------
// CSV export links -- point at this app's own `/reports/csv/[report]` route
// handler (NOT admin-api directly), which forwards the caller's session
// cookie server-side and streams the bytes back. A raw `<a href>` to
// admin-api cannot work here: admin-api reads the `access_token` cookie a
// same-origin browser request to *this* app carries, not one for admin-
// api's own origin (the same reason `adminApiFetch` forwards it manually
// server-to-server, lib/api.ts) -- see reports/csv/[report]/route.ts's
// header comment for the full explanation.
// ---------------------------------------------------------------------------

export type ReportName =
  | "leads-by-stage"
  | "bookings"
  | "funnel"
  | "win-loss"
  | "lead-sources"
  | "score-distribution"
  | "agent-performance"
  | "recent-conversions";

export function reportCsvPath(report: ReportName, query: string, tenantId?: string): string {
  const params = new URLSearchParams(query);
  if (tenantId) params.set("tenant_id", tenantId);
  return `/reports/csv/${report}?${params.toString()}`;
}
