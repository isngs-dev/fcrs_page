/**
 * Server-only data layer for the conversation analytics dashboard (S13.5).
 * Mirrors `lib/leads.ts`'s shape: a pure, unit-testable query resolver +
 * a `getAnalyticsOverview()` that maps the backend's nested aggregate (or
 * any error) into a discriminated `AnalyticsResult` the page renders --
 * no silent fallbacks (CLAUDE.md §3): a backend error always becomes a
 * visible, honest state, and a `null` rate is NEVER coerced to `0`.
 *
 * Constants below are sourced verbatim from the real backend, not invented:
 *  - `ANALYTICS_BUCKETS` mirrors `_VALID_BUCKETS`
 *    (services/api/src/api/analytics/repository.py:46) -- `{day, week,
 *    month}` since SR-9.5 D3 (a deliberate, disclosed widening of this
 *    shipped endpoint's contract -- `month` used to 422; `hour` is still
 *    rejected, explicitly out of scope).
 *  - The response shape mirrors `AnalyticsOverviewResponse`
 *    (services/api/src/api/analytics/routes.py:78-89) exactly.
 *  - `ANALYTICS_RANGES` is a UI choice (S13.5.md decision 5, Q1 default
 *    30d, matching the backend's `analytics_default_window_days`), well
 *    within the backend's `analytics_max_window_days` (366) cap.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** Pure constants/types re-exported from `lib/analytics-constants.ts` (kept
 * free of `server-only` so `analytics-range.tsx`, a Client Component, can
 * import them too) -- see that file's header comment for why. */
import {
  ANALYTICS_BUCKETS,
  ANALYTICS_RANGES,
  CUSTOM_RANGE_KEY,
  DEFAULT_RANGE_KEY,
  DEFAULT_BUCKET,
  type AnalyticsBucket,
  type AnalyticsRangeKey,
} from "@/lib/analytics-constants";
export { ANALYTICS_BUCKETS, ANALYTICS_RANGES, CUSTOM_RANGE_KEY, DEFAULT_RANGE_KEY, DEFAULT_BUCKET };
export type { AnalyticsBucket, AnalyticsRangeKey };

/** Camel-cased mirror of `AnalyticsOverviewResponse`
 * (routes.py:78-89). `null` rates are preserved as `null` -- never
 * coerced to `0` (Decision 6, the load-bearing no-silent-fallback
 * property). No `tenant_id`/`visitor_id`/`conversation_id` -- the backend
 * response is already leak-free by construction (S11.2 decision 9). */
export interface AnalyticsOverview {
  window: {
    from: string;
    to: string;
    bucket: string;
  };
  totals: {
    conversations: number;
    userTurns: number;
    botTurns: number;
    decidedBotTurns: number;
  };
  intentDistribution: Record<string, number>;
  decisionDistribution: Record<string, number>;
  fallbackRate: number | null;
  deflectionRate: number | null;
  groundedRate: number | null;
  schedule: {
    ctaConversations: number;
    conversions: number;
    conversionRate: number | null;
  };
  series: {
    bucketStart: string;
    conversations: number;
    answers: number;
    escalations: number;
    bookings: number;
  }[];
}

interface AnalyticsOverviewResponseBody {
  window: { from: string; to: string; bucket: string };
  totals: {
    conversations: number;
    user_turns: number;
    bot_turns: number;
    decided_bot_turns: number;
  };
  intent_distribution: Record<string, number>;
  decision_distribution: Record<string, number>;
  fallback_rate: number | null;
  deflection_rate: number | null;
  grounded_rate: number | null;
  schedule: {
    cta_conversations: number;
    conversions: number;
    conversion_rate: number | null;
  };
  series: {
    bucket_start: string;
    conversations: number;
    answers: number;
    escalations: number;
    bookings: number;
  }[];
}

export type AnalyticsResult =
  | { status: "ok"; data: AnalyticsOverview }
  | { status: "error"; message: string; correlationId: string };

export interface AnalyticsQueryParams {
  range?: string;
  bucket?: string;
  /** Custom-range inputs (5b "Custom" toggle option), `YYYY-MM-DD` from a
   * native `<input type="date">`. Only consulted when `range === "custom"`. */
  from?: string;
  to?: string;
}

function isValidCustomRange(fromRaw: string | undefined, toRaw: string | undefined): boolean {
  if (!fromRaw || !toRaw) return false;
  const from = new Date(fromRaw);
  const to = new Date(toRaw);
  if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) return false;
  return from.getTime() < to.getTime();
}

/**
 * Pure, unit-testable range/query resolver (decision 3/5). Drops an
 * unknown/blank `range` -> default `30d`; drops an unknown/blank `bucket`
 * -> default `day` (so a stray/hand-edited `?bucket=month` degrades to
 * "day" rather than a guaranteed `422 INVALID_BUCKET` on first paint --
 * Decision 5's belt-and-suspenders is the 422 mapping in
 * `getAnalyticsOverview` below, for the rare case a bad value still
 * reaches the backend). Computes `to = now(UTC)` / `from = to - days` for a
 * preset range and returns a URL-encoded query string via `URLSearchParams`
 * (never string-concatenated raw).
 *
 * `range=custom` (5b's 4th toggle option): honors caller-supplied `from`/
 * `to` (`YYYY-MM-DD`) verbatim as the window bounds when both are present
 * and `from < to` -- no-silent-fallback (CLAUDE.md §3): an incomplete or
 * invalid custom range does NOT get silently coerced into some other
 * window; it falls back to the same explicit `30d` default as an unknown
 * preset key, so the caller sees a real, disclosed window rather than a
 * custom range that silently wasn't applied.
 */
export function resolveAnalyticsQuery(params: AnalyticsQueryParams): string {
  const rangeKey = params.range?.trim();

  let from: Date;
  let to: Date;

  if (rangeKey === CUSTOM_RANGE_KEY && isValidCustomRange(params.from, params.to)) {
    from = new Date(params.from!);
    to = new Date(params.to!);
  } else {
    const range =
      ANALYTICS_RANGES.find((r) => r.key === rangeKey) ??
      ANALYTICS_RANGES.find((r) => r.key === DEFAULT_RANGE_KEY)!;
    to = new Date();
    from = new Date(to.getTime() - range.days * 24 * 60 * 60 * 1000);
  }

  const bucketValue = params.bucket?.trim();
  const bucket =
    bucketValue && (ANALYTICS_BUCKETS as readonly string[]).includes(bucketValue)
      ? (bucketValue as AnalyticsBucket)
      : DEFAULT_BUCKET;

  const query = new URLSearchParams();
  query.set("from", from.toISOString());
  query.set("to", to.toISOString());
  query.set("bucket", bucket);

  return query.toString();
}

function toAnalyticsOverview(body: AnalyticsOverviewResponseBody): AnalyticsOverview {
  return {
    window: {
      from: body.window.from,
      to: body.window.to,
      bucket: body.window.bucket,
    },
    totals: {
      conversations: body.totals.conversations,
      userTurns: body.totals.user_turns,
      botTurns: body.totals.bot_turns,
      decidedBotTurns: body.totals.decided_bot_turns,
    },
    intentDistribution: body.intent_distribution,
    decisionDistribution: body.decision_distribution,
    // `null` preserved as `null` -- never coerced to `0` (Decision 6a).
    fallbackRate: body.fallback_rate,
    deflectionRate: body.deflection_rate,
    groundedRate: body.grounded_rate,
    schedule: {
      ctaConversations: body.schedule.cta_conversations,
      conversions: body.schedule.conversions,
      conversionRate: body.schedule.conversion_rate,
    },
    series: body.series.map((b) => ({
      bucketStart: b.bucket_start,
      conversations: b.conversations,
      answers: b.answers,
      escalations: b.escalations,
      bookings: b.bookings,
    })),
  };
}

/**
 * Fetch the caller's tenant conversation-analytics overview for the
 * resolved range/bucket. Never sends a `tenant_id` -- scoping is entirely
 * the backend's repository-layer job (CLAUDE.md §3). Never logs the
 * response body (PII-minimal; the response is already PII-free by
 * construction).
 *
 * `tenantId` (S13.7): when provided, targets the S12.7 PLATFORM_ADMIN
 * super-user surface `GET /admin/tenants/{tenantId}/analytics/overview`
 * instead of the implicit `GET /admin/analytics/overview` -- same response
 * shape (routes.py `_get_overview`), only the URL prefix differs. Always
 * the route segment's `{tenantId}`, never client state (D1).
 */
export async function getAnalyticsOverview(
  params: AnalyticsQueryParams,
  tenantId?: string
): Promise<AnalyticsResult> {
  const query = resolveAnalyticsQuery(params);
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/analytics/overview`
    : "/admin/analytics/overview";

  try {
    const response = await adminApiFetch(`${basePath}?${query}`);
    const body = (await response.json()) as AnalyticsOverviewResponseBody;
    return { status: "ok", data: toAnalyticsOverview(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    // A network throw (not an AdminApiError) -- the request never reached
    // (or never returned from) admin-api.
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view analytics.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
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

/**
 * Format a `float | None` rate for display (Decision 6a, MANDATORY
 * no-silent-fallback): `null` -> "No data" (a zero-denominator rate is
 * genuinely unknown, NEVER a fabricated "0%"); a real number (including
 * `0`) -> a percentage to 1 decimal place, trailing `.0` trimmed
 * (`0 -> "0%"`, `0.4213 -> "42.1%"`, `1 -> "100%"`).
 */
export function formatRate(rate: number | null): string {
  if (rate === null) return "No data";
  const pct = rate * 100;
  const rounded = Math.round(pct * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)}%`;
}
