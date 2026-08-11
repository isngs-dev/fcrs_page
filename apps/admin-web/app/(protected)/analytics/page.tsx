/**
 * Conversation analytics dashboard (S13.5), restyled to match the current
 * reference (`Console.dc.html:558-676`, formerly cited against a
 * superseded HANDOFF-SPEC.md screen 5b / "citron" design system -- SR-27
 * re-points this docstring at the live reference): 4 stat cards/gauges
 * (last one ink), conversation-volume area+line chart, booking-funnel
 * bars, decision-mix donut, top-intents list, peak-hours + top-questions
 * honest gap cards. Range toggle 7d/30d/90d/custom. CLIENT_ADMIN +
 * CLIENT_AGENT -- gated by the EXISTING `requireAnyRole` (decision 2), no
 * new auth helper.
 *
 * SERVER-FIRST (decision 1): this is an `async` server component that reads
 * range/bucket state from the URL `searchParams`, fetches the aggregate
 * once per navigation via `getAnalyticsOverview`, and renders it. No client
 * state, no polling -- changing the range or bucket is a plain URL
 * navigation (a GET `<form>`) that re-runs this component.
 *
 * Honest per-chart accounting (this restyle does not invent data --
 * CLAUDE.md §3 no-silent-fallback):
 *  - Stat cards/gauges, volume chart, funnel, decision-mix donut, "Top
 *    intents": every number is a real field off `AnalyticsOverview` (see
 *    each component's doc comment for the exact backend source).
 *  - "Top questions" (literal question text) and "Peak hours" (hourly
 *    volume): NOT backed by any real metric this codebase computes today
 *    (`intent_distribution` is category-level, not per-question; the
 *    repository buckets by `day`/`week`/`month`, never by hour or
 *    day-of-week) -- G13/G14, re-verified FRESH at SR-30 planning time
 *    (`analytics/repository.py:46`'s `_VALID_BUCKETS`,
 *    `routes.py:80-91`'s response model), still open. Rendered as honest
 *    `UnavailableCard`s naming the specific gap, instead of fabricated
 *    bars.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import {
  ANALYTICS_BUCKETS,
  ANALYTICS_RANGES,
  CUSTOM_RANGE_KEY,
  DEFAULT_BUCKET,
  DEFAULT_RANGE_KEY,
  getAnalyticsOverview,
  type AnalyticsBucket,
  type AnalyticsRangeKey,
} from "@/lib/analytics";
import { AnalyticsRange } from "@/app/(protected)/analytics/analytics-range";
import { AnalyticsCards } from "@/app/(protected)/analytics/analytics-cards";
import { VolumeAreaChart } from "@/app/(protected)/analytics/volume-area-chart";
import { FunnelBars } from "@/app/(protected)/analytics/funnel-bars";
import { DistributionBars } from "@/app/(protected)/analytics/distribution-bars";
import { DecisionMixDonut } from "@/app/(protected)/analytics/decision-mix-donut";
import { SeriesTable } from "@/app/(protected)/analytics/series-table";
import { UnavailableCard } from "@/app/(protected)/analytics/unavailable-card";

interface AnalyticsPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = [
  ...ANALYTICS_RANGES.map((r) => r.key),
  CUSTOM_RANGE_KEY,
];

export default async function AnalyticsPage({ searchParams }: AnalyticsPageProps) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawRange = firstValue(params.range);
  const range: AnalyticsRangeKey = ALL_RANGE_KEYS.includes(rawRange ?? "")
    ? (rawRange as AnalyticsRangeKey)
    : DEFAULT_RANGE_KEY;
  const rawBucket = firstValue(params.bucket);
  const bucket: AnalyticsBucket =
    rawBucket && (ANALYTICS_BUCKETS as readonly string[]).includes(rawBucket)
      ? (rawBucket as AnalyticsBucket)
      : DEFAULT_BUCKET;
  const from = firstValue(params.from);
  const to = firstValue(params.to);

  const result = await getAnalyticsOverview({ range, bucket, from, to });

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <Link href="/" className="text-sm text-[var(--muted-foreground)] hover:underline">
        ← Back to console
      </Link>

      <div className="flex flex-wrap items-end justify-between gap-3.5">
        <div>
          <h1 className="text-[28px] font-semibold text-[var(--foreground)]">Analytics</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Conversation volume, deflection, and booking outcomes in this window.
          </p>
        </div>
        <AnalyticsRange currentRange={range} currentBucket={bucket} currentFrom={from} currentTo={to} />
      </div>

      {result.status === "error" ? (
        <p
          role="alert"
          className="rounded-[14px] border border-[var(--danger-fg)]/40 bg-[#f6e3df]/60 p-4 text-sm text-[var(--danger-fg)]"
        >
          {result.message}
          {result.correlationId ? (
            <span className="block text-xs text-[var(--danger-fg)]/80">
              Correlation ID: {result.correlationId}
            </span>
          ) : null}
        </p>
      ) : result.data.totals.conversations === 0 ? (
        <p
          role="status"
          className="rounded-[14px] border border-[var(--border)] bg-[var(--secondary)] p-4 text-sm text-[var(--ink-2)]"
        >
          No conversation activity in this window yet.
        </p>
      ) : (
        <>
          <AnalyticsCards data={result.data} />

          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[1.5fr_1fr]">
            <div className="flex flex-col gap-4">
              <VolumeAreaChart data={result.data} />
              <div className="rounded-[14px] border border-[var(--border)] p-5">
                <FunnelBars data={result.data} />
              </div>
            </div>
            <div className="flex flex-col gap-4">
              <DistributionBars title="Top intents" data={result.data.intentDistribution} />
              <UnavailableCard
                title="Top questions"
                reason="This tenant's analytics only track intent categories (e.g. “pricing”), not verbatim question text -- see “Top intents” for the closest real signal. Adding per-question tracking needs a new backend metric."
              />
              <UnavailableCard
                title="Peak hours"
                reason="The analytics repository only aggregates by day, week, or month -- never by hour or day-of-week (no hourly bucket exists in services/api/src/api/analytics/repository.py)."
              />
            </div>
          </div>

          <DecisionMixDonut data={result.data.decisionDistribution} />

          <div className="flex flex-col gap-2">
            <h2 className="text-sm font-bold text-[var(--foreground)]">Time series</h2>
            <SeriesTable series={result.data.series} />
          </div>
        </>
      )}
    </div>
  );
}
