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
        <legend className="text-xs font-medium text-[#70716a]">Date range</legend>
        <div className="flex overflow-hidden rounded-[9px] border border-[#e7e7e2] text-xs font-semibold">
          {ANALYTICS_RANGES.map((range) => (
            <label
              key={range.key}
              className="cursor-pointer px-3.5 py-[7px] text-[#5a5b54] transition-colors has-checked:bg-[#191a17] has-checked:text-white hover:has-[:not(:checked)]:bg-[#f7f7f3] focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-[#191a17]"
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
          <label htmlFor="bucket" className="text-xs font-medium text-[#70716a]">
            Bucket
          </label>
          <select
            id="bucket"
            name="bucket"
            defaultValue={currentBucket ?? DEFAULT_REPORT_BUCKET}
            className="h-8 rounded-[9px] border border-[#e7e7e2] bg-white px-2.5 py-1 text-sm text-[#191a17] outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
          >
            {REPORT_BUCKETS.map((bucket) => (
              <option key={bucket} value={bucket}>
                {BUCKET_LABELS[bucket]}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      <button
        type="submit"
        className="h-8 rounded-[9px] bg-[#191a17] px-3.5 text-sm font-semibold text-[#e4f222] transition-colors hover:bg-[#30312d] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
      >
        Apply
      </button>
    </form>
  );
}
