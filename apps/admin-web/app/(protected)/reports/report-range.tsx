/**
 * Range control shared by all four report pages (D10): a plain GET `<form>`,
 * URL-driven, no client state -- mirrors `analytics/analytics-range.tsx`'s
 * segmented-pill pattern exactly (native radio inputs styled as pills, zero
 * JS). `showBucket` renders the bucket selector too (bookings report only,
 * D3's month option included).
 */
import { ANALYTICS_RANGES } from "@/lib/analytics";
import { REPORT_BUCKETS, DEFAULT_REPORT_BUCKET, type ReportBucket } from "@/lib/reports";

const BUCKET_LABELS: Record<ReportBucket, string> = {
  day: "Day",
  week: "Week",
  month: "Month",
};

export function ReportRange({
  basePath,
  currentRange,
  currentBucket,
  showBucket = false,
}: {
  basePath: string;
  currentRange: string;
  currentBucket?: ReportBucket;
  showBucket?: boolean;
}) {
  return (
    <form
      action={basePath}
      method="get"
      className="flex flex-wrap items-end gap-3"
      aria-label="Report date range"
    >
      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs font-medium text-[var(--muted-foreground)]">Date range</legend>
        <div className="flex overflow-hidden rounded-[9px] border border-[var(--border)] text-xs font-semibold">
          {ANALYTICS_RANGES.map((range) => (
            <label
              key={range.key}
              className="cursor-pointer px-3.5 py-[7px] text-[var(--ink-2)] transition-colors has-checked:bg-[var(--foreground)] has-checked:text-white hover:has-[:not(:checked)]:bg-[var(--secondary)] focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-[var(--foreground)]"
            >
              <input
                type="radio"
                name="range"
                value={range.key}
                defaultChecked={currentRange === range.key}
                className="sr-only"
              />
              {range.label.replace("Last ", "")}
            </label>
          ))}
        </div>
      </fieldset>

      {showBucket ? (
        <div className="flex flex-col gap-1">
          <label htmlFor="bucket" className="text-xs font-medium text-[var(--muted-foreground)]">
            Bucket
          </label>
          <select
            id="bucket"
            name="bucket"
            defaultValue={currentBucket ?? DEFAULT_REPORT_BUCKET}
            className="h-8 rounded-[9px] border border-[var(--border)] bg-white px-2.5 py-1 text-sm text-[var(--foreground)] outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--foreground)]"
          >
            {REPORT_BUCKETS.map((bucket) => (
              <option key={bucket} value={bucket}>
                {BUCKET_LABELS[bucket]}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      {/* SR-15 D1: citron button text deleted, re-decided to white -- mirrors
          analytics-range.tsx's identical control. */}
      <button
        type="submit"
        className="h-8 rounded-[9px] bg-[var(--foreground)] px-3.5 text-sm font-semibold text-white transition-colors hover:bg-[var(--ink-2)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--foreground)]"
      >
        Apply
      </button>
    </form>
  );
}
