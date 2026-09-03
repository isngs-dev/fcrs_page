/**
 * Outcome/ROI Dashboard v1 trend chart: leads generated vs. appointments
 * booked, both bucketed over the same window. No reusable time-series chart
 * exists in this codebase (`BookingsBars`/`VolumeAreaChart` both hardcode
 * their own field names) so this follows the same house style instead:
 * raw inline SVG, `role="img"` + `aria-label`, `sr-only` table fallback.
 *
 * "Appointments booked" uses `totalExcludingCancelled` -- the same "real
 * bookings" figure already shown as the bookings report's headline KPI --
 * not the raw `booked` status count, which would drop bookings that have
 * since moved to completed/no-show.
 */
import type { LeadsOverTimeReport, BookingsReport } from "@/lib/reports";

function formatBucketLabel(iso: string, bucket: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  if (bucket === "month") {
    return date.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
  }
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

interface TrendPoint {
  bucketStart: string;
  leads: number;
  bookings: number;
}

function mergeSeries(leads: LeadsOverTimeReport, bookings: BookingsReport): TrendPoint[] {
  const leadsByBucket = new Map(leads.series.map((b) => [b.bucketStart, b.count]));
  const bookingsByBucket = new Map(bookings.series.map((b) => [b.bucketStart, b.totalExcludingCancelled]));
  const bucketStarts = Array.from(new Set([...leadsByBucket.keys(), ...bookingsByBucket.keys()])).sort();
  return bucketStarts.map((bucketStart) => ({
    bucketStart,
    leads: leadsByBucket.get(bucketStart) ?? 0,
    bookings: bookingsByBucket.get(bucketStart) ?? 0,
  }));
}

const WIDTH = 640;
const HEIGHT = 200;
const PAD_X = 8;
const PAD_Y = 12;

function linePath(points: TrendPoint[], key: "leads" | "bookings", maxValue: number): string {
  if (points.length === 0) return "";
  const stepX = points.length > 1 ? (WIDTH - PAD_X * 2) / (points.length - 1) : 0;
  return points
    .map((p, i) => {
      const x = PAD_X + i * stepX;
      const y = HEIGHT - PAD_Y - (p[key] / maxValue) * (HEIGHT - PAD_Y * 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

const SERIES = [
  { key: "leads" as const, label: "leads generated", color: "var(--foreground)" },
  { key: "bookings" as const, label: "appointments booked", color: "#3f7d57" },
];

export function RoiTrendChart({
  leads,
  bookings,
}: {
  leads: LeadsOverTimeReport;
  bookings: BookingsReport;
}) {
  const points = mergeSeries(leads, bookings);

  if (points.length === 0) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        No data in this window.
      </p>
    );
  }

  const maxValue = Math.max(...points.flatMap((p) => [p.leads, p.bookings]), 1);
  const bucket = leads.window.bucket;

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

      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-[200px] w-full"
        role="img"
        aria-label={`Leads generated vs. appointments booked per ${bucket}: ${points
          .map((p) => `${formatBucketLabel(p.bucketStart, bucket)} -- ${p.leads} leads, ${p.bookings} bookings`)
          .join("; ")}`}
      >
        {SERIES.map((s) => (
          <path
            key={s.key}
            d={linePath(points, s.key, maxValue)}
            fill="none"
            stroke={s.color}
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}
      </svg>

      <div className="flex justify-between text-[10.5px] text-[var(--muted-foreground)]">
        <span>{formatBucketLabel(points[0]!.bucketStart, bucket)}</span>
        {points.length > 1 ? (
          <span>{formatBucketLabel(points[points.length - 1]!.bucketStart, bucket)}</span>
        ) : null}
      </div>

      <table className="sr-only">
        <caption>Leads generated vs. appointments booked per {bucket}</caption>
        <thead>
          <tr>
            <th scope="col">Bucket</th>
            <th scope="col">Leads generated</th>
            <th scope="col">Appointments booked</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.bucketStart}>
              <td>{formatBucketLabel(p.bucketStart, bucket)}</td>
              <td>{p.leads}</td>
              <td>{p.bookings}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
