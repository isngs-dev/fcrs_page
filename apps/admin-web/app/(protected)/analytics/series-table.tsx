/**
 * Renders the time-bucketed `series` as a table, restyled to the reference
 * design (`Console.dc.html`'s `.tbl-card`/`.th`/`.td` table recipe: header
 * row `--cream` background, 11px/600 uppercase muted; rows 13px, row-line
 * dividers). Doubles as the accessible text-alternative for
 * `VolumeAreaChart` and `FunnelBars` (ui-ux-pro-max Charts & Data: a table
 * alternative for screen readers) -- exact per-bucket counts, not just
 * chart shapes. (SR-27 slice 9: `WeeklyBars` was retired/deleted in favor
 * of `VolumeAreaChart`; this doc comment is updated to match.)
 */
import type { AnalyticsOverview } from "@/lib/analytics";

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function SeriesTable({ series }: { series: AnalyticsOverview["series"] }) {
  if (series.length === 0) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        No time-series data for this window.
      </p>
    );
  }

  return (
    <div className="overflow-hidden rounded-[14px] border border-[var(--border)]">
      <table className="w-full border-collapse text-[13px]">
        <caption className="sr-only">
          Conversation activity per bucket: exact counts backing the charts above.
        </caption>
        <thead>
          <tr className="bg-[var(--secondary)]">
            <th
              scope="col"
              className="px-4 py-2.5 text-left text-[11.5px] font-semibold tracking-wide text-[var(--muted-foreground)] uppercase"
            >
              Bucket start
            </th>
            <th
              scope="col"
              className="px-4 py-2.5 text-right text-[11.5px] font-semibold tracking-wide text-[var(--muted-foreground)] uppercase"
            >
              Conversations
            </th>
            <th
              scope="col"
              className="px-4 py-2.5 text-right text-[11.5px] font-semibold tracking-wide text-[var(--muted-foreground)] uppercase"
            >
              Answers
            </th>
            <th
              scope="col"
              className="px-4 py-2.5 text-right text-[11.5px] font-semibold tracking-wide text-[var(--muted-foreground)] uppercase"
            >
              Escalations
            </th>
            <th
              scope="col"
              className="px-4 py-2.5 text-right text-[11.5px] font-semibold tracking-wide text-[var(--muted-foreground)] uppercase"
            >
              Bookings
            </th>
          </tr>
        </thead>
        <tbody>
          {series.map((bucket) => (
            <tr key={bucket.bucketStart} className="border-t border-[var(--secondary)]">
              <td className="px-4 py-2.5 font-medium text-[var(--foreground)]">
                {formatDate(bucket.bucketStart)}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums text-[var(--ink-2)]">
                {bucket.conversations}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums text-[var(--ink-2)]">
                {bucket.answers}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums text-[var(--ink-2)]">
                {bucket.escalations}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums text-[var(--ink-2)]">
                {bucket.bookings}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
