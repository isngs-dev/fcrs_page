/**
 * Conversation-volume filled area + line chart (SR-27 slice 9, per SR-23's
 * spec re-confirmed): SVG path fill `rgba(51,51,51,.07)` under a polyline
 * stroked `#333333`, 3 gridlines + baseline. Replaces `weekly-bars.tsx`'s
 * grouped bars, using the same real `series` array (conversations/answers/
 * escalations/bookings, `routes.py:38-45`) `weekly-bars.tsx` already used --
 * this component plots `conversations` as the area/line series (the
 * reference's single "Conversation volume" chart, not a two-series
 * comparison); `weekly-bars.tsx` is retired by this change (see
 * `page.tsx`'s docstring).
 */
import type { AnalyticsOverview } from "@/lib/analytics";

const WIDTH = 600;
const HEIGHT = 180;
const PADDING_X = 8;
const PADDING_Y = 12;

function formatBucketLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function VolumeAreaChart({ data }: { data: AnalyticsOverview }) {
  const { series } = data;

  if (series.length === 0) {
    return (
      <div className="flex flex-col gap-4 rounded-[14px] border border-[var(--border)] p-5">
        <span className="text-sm font-bold text-[var(--foreground)]">Conversation volume</span>
        <p role="status" className="text-sm text-[var(--muted-foreground)]">
          No time-series data for this window.
        </p>
      </div>
    );
  }

  const maxValue = Math.max(...series.map((b) => b.conversations), 1);
  const plotWidth = WIDTH - PADDING_X * 2;
  const plotHeight = HEIGHT - PADDING_Y * 2;

  const points = series.map((b, i) => {
    const x = PADDING_X + (series.length === 1 ? plotWidth / 2 : (i / (series.length - 1)) * plotWidth);
    const y = PADDING_Y + plotHeight - (b.conversations / maxValue) * plotHeight;
    return { x, y, bucket: b };
  });

  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L${points[points.length - 1].x.toFixed(1)},${(PADDING_Y + plotHeight).toFixed(1)} L${points[0].x.toFixed(1)},${(PADDING_Y + plotHeight).toFixed(1)} Z`;

  // 3 gridlines + baseline (4 horizontal lines total), per the reference.
  const gridlineYs = [0, 1 / 3, 2 / 3, 1].map((f) => PADDING_Y + plotHeight * f);

  const ariaLabel = `Conversation volume per ${data.window.bucket}: ${series
    .map((b) => `${formatBucketLabel(b.bucketStart)} -- ${b.conversations} conversations`)
    .join("; ")}`;

  return (
    <div className="flex flex-col gap-4 rounded-[14px] border border-[var(--border)] p-5">
      <span className="text-sm font-bold text-[var(--foreground)]">Conversation volume</span>

      <div role="img" aria-label={ariaLabel} className="w-full">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full" preserveAspectRatio="none" style={{ height: 180 }}>
          {gridlineYs.map((y) => (
            <line key={y} x1={PADDING_X} x2={WIDTH - PADDING_X} y1={y} y2={y} stroke="#e6e6e6" strokeWidth={1} />
          ))}
          <path d={areaPath} fill="rgba(51,51,51,.07)" stroke="none" />
          <path d={linePath} fill="none" stroke="#333333" strokeWidth={2} />
        </svg>
      </div>

      <div className="flex justify-between text-[10.5px] text-[var(--muted-foreground)]">
        {series.map((b) => (
          <span key={b.bucketStart}>{formatBucketLabel(b.bucketStart)}</span>
        ))}
      </div>

      <table className="sr-only">
        <caption>Conversation volume per {data.window.bucket}</caption>
        <thead>
          <tr>
            <th scope="col">Bucket</th>
            <th scope="col">Conversations</th>
          </tr>
        </thead>
        <tbody>
          {series.map((b) => (
            <tr key={b.bucketStart}>
              <td>{formatBucketLabel(b.bucketStart)}</td>
              <td>{b.conversations}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
