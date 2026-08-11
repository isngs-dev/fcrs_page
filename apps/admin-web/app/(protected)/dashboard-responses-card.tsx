/**
 * SR-16 scope item 3 -- "Responses over time" (M6, D4). Two-series bars
 * (Answered `--primary` / Escalated `--muted-foreground`) over the existing
 * `series[]`, URL-driven bucket selector offering ONLY `day`/`week`
 * (`HUB_BUCKETS` -- D4 explicitly excludes `month`).
 *
 * SR-23: the plot itself moved into `ResponsesOverTimeChart`, a client island
 * (it needs hover/focus state for the reference's tooltip). THIS file stays a
 * server component -- card chrome, legend and the no-JS bucket selector are
 * all still server-rendered, and the selector remains a GET form that re-runs
 * the page via the URL rather than client state.
 *
 * Card geometry follows the reference artboard `Dashboard.dc.html:146-156`:
 * `.soft-card`, `padding:14px 20px 10px`, a 16px/600 heading, and a header
 * row carrying the 9px legend dots plus the bucket control on the right.
 */
import { SoftCard } from "@/components/admin/soft-card";
import { HUB_BUCKETS, type ChatbotHub, type HubBucket, type HubPeriod } from "@/lib/hub";
import {
  ResponsesOverTimeChart,
  type ResponsesChartBucket,
} from "@/app/(protected)/dashboard-responses-chart";

function formatBucketLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function formatShortBucketLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function ResponsesOverTimeCard({ hub }: { hub: ChatbotHub }) {
  const { series } = hub.analytics;
  const buckets: ResponsesChartBucket[] = series.map((entry) => ({
    bucketStart: entry.bucketStart,
    answers: entry.answers,
    escalations: entry.escalations,
    label: formatBucketLabel(entry.bucketStart),
    shortLabel: formatShortBucketLabel(entry.bucketStart),
  }));

  return (
    <SoftCard as="section" aria-label="Responses over time" className="px-5 pt-3.5 pb-2.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-base font-semibold text-[var(--ink)]">Responses over time</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-3.5 text-xs text-[var(--ink-2)]">
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className="size-[9px] rounded-full bg-primary" />
              Answered
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className="size-[9px] rounded-full bg-muted-foreground" />
              Escalated
            </span>
          </div>
          <BucketSelector period={hub.period} bucket={hub.bucket} />
        </div>
      </div>

      {buckets.length === 0 ? (
        <p
          role="status"
          className="mt-5 rounded-lg border border-dashed border-border bg-secondary p-4 text-sm text-muted-foreground"
        >
          No conversations yet in this period.
        </p>
      ) : (
        <ResponsesOverTimeChart buckets={buckets} bucket={hub.bucket} />
      )}
    </SoftCard>
  );
}

function BucketSelector({ period, bucket }: { period: HubPeriod; bucket: HubBucket }) {
  return (
    <form action="/" method="get" className="flex items-center gap-2" aria-label="Responses chart bucket">
      <input type="hidden" name="period" value={period} />
      <fieldset className="flex overflow-hidden rounded-lg border border-border text-xs font-semibold">
        <legend className="sr-only">Bucket</legend>
        {HUB_BUCKETS.map((option: HubBucket) => (
          <label
            key={option}
            className="cursor-pointer px-2.5 py-1.5 capitalize text-[var(--ink-2)] transition-colors has-checked:bg-primary has-checked:text-primary-foreground focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-ring"
          >
            <input type="radio" name="bucket" value={option} defaultChecked={bucket === option} className="sr-only" />
            {option}
          </label>
        ))}
      </fieldset>
      <button
        type="submit"
        className="min-h-8 rounded-lg border border-border bg-card px-2.5 text-xs font-semibold text-[var(--ink-2)] hover:bg-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        Apply
      </button>
    </form>
  );
}
