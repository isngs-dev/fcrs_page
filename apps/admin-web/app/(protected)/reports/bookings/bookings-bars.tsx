/**
 * Bookings grouped bars (SR-9.5 D5/D10): booked/completed/no-show/cancelled
 * per bucket, mirroring `analytics/weekly-bars.tsx`'s shape. Cancelled is
 * its OWN visible series -- never hidden, unlike the legacy `overview`
 * series' `status='booked'`-only narrowing.
 */
import type { BookingsReport } from "@/lib/reports";

function formatBucketLabel(iso: string, bucket: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  if (bucket === "month") {
    return date.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
  }
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// SR-15 D1: "completed"'s citron fill is deleted and re-decided to the
// design's success-fg green -- completed is this report's "good" booking
// outcome, mirroring the reports stage-bars/funnel-steps re-decisions.
const SERIES = [
  { key: "booked" as const, label: "booked", color: "var(--foreground)" },
  { key: "completed" as const, label: "completed", color: "#3f7d57" },
  { key: "noShow" as const, label: "no-show", color: "var(--muted-foreground)" },
  { key: "cancelled" as const, label: "cancelled", color: "#a24b4b" },
];

export function BookingsBars({ data }: { data: BookingsReport }) {
  const { series } = data;

  if (series.length === 0) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        No data in this window.
      </p>
    );
  }

  const maxValue = Math.max(
    ...series.flatMap((b) => SERIES.map((s) => b[s.key])),
    1
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 text-[11px] text-[var(--muted-foreground)]">
        {SERIES.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: s.color }} />
            {s.label}
          </span>
        ))}
      </div>

      <div
        className="flex h-[200px] items-end gap-3 overflow-x-auto"
        role="img"
        aria-label={`Bookings per ${data.window.bucket}: ${series
          .map(
            (b) =>
              `${formatBucketLabel(b.bucketStart, data.window.bucket)} -- ${b.booked} booked, ${b.completed} completed, ${b.noShow} no-show, ${b.cancelled} cancelled`
          )
          .join("; ")}`}
      >
        {series.map((b) => (
          <div key={b.bucketStart} className="flex h-full min-w-[64px] flex-1 flex-col items-center justify-end gap-1.5">
            <div
              className="flex h-[170px] w-full items-end justify-center gap-1"
              title={`${formatBucketLabel(b.bucketStart, data.window.bucket)}: ${b.booked} booked, ${b.completed} completed, ${b.noShow} no-show, ${b.cancelled} cancelled`}
            >
              {SERIES.map((s) => {
                const value = b[s.key];
                const pct = Math.max((value / maxValue) * 100, value > 0 ? 4 : 0);
                return (
                  <div
                    key={s.key}
                    className="w-3 rounded-[3px]"
                    style={{ height: `${pct}%`, backgroundColor: s.color }}
                  />
                );
              })}
            </div>
            <span className="text-[10.5px] text-[var(--muted-foreground)]">
              {formatBucketLabel(b.bucketStart, data.window.bucket)}
            </span>
          </div>
        ))}
      </div>

      <table className="sr-only">
        <caption>Bookings per {data.window.bucket}</caption>
        <thead>
          <tr>
            <th scope="col">Bucket</th>
            <th scope="col">Booked</th>
            <th scope="col">Completed</th>
            <th scope="col">No-show</th>
            <th scope="col">Cancelled</th>
            <th scope="col">Total excluding cancelled</th>
          </tr>
        </thead>
        <tbody>
          {series.map((b) => (
            <tr key={b.bucketStart}>
              <td>{formatBucketLabel(b.bucketStart, data.window.bucket)}</td>
              <td>{b.booked}</td>
              <td>{b.completed}</td>
              <td>{b.noShow}</td>
              <td>{b.cancelled}</td>
              <td>{b.totalExcludingCancelled}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
