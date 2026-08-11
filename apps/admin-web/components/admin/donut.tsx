/**
 * Shared donut-chart primitive (SR-27 slice 9), genericized from
 * `reports/lead-sources/lead-sources-donut.tsx`'s SVG `stroke-dasharray`
 * arc math (SR-19 D4/D5) so Analytics' "Decision mix" donut and Reports'
 * "Lead sources" donut share one implementation instead of two copies of
 * the same arc math drifting apart over time. No charting library (D5
 * still applies) -- hand-rolled SVG circles, `role="img"` + a full
 * `aria-label`, a visually-hidden `<table>` of exact values, color never
 * the sole carrier of meaning (every slice has a text label + count).
 */
const DEFAULT_SLICE_COLORS = [
  "var(--foreground)",
  "var(--ink-2)",
  "var(--muted-foreground)",
  "#a9aa9f",
  "#d5d5cb",
  "#8a6d3b",
] as const;

const RADIUS = 52;
const STROKE_WIDTH = 18;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

export interface DonutSlice {
  label: string;
  count: number;
}

export function Donut({
  slices,
  total,
  centerCaption,
  ariaLabelPrefix,
  emptyMessage,
  colors = DEFAULT_SLICE_COLORS,
}: {
  slices: DonutSlice[];
  total: number;
  /** e.g. "leads" / "conversations" -- printed under the center total. */
  centerCaption: string;
  /** e.g. "Lead sources" / "Decision mix" -- prefixes the aria-label. */
  ariaLabelPrefix: string;
  emptyMessage: string;
  colors?: readonly string[];
}) {
  if (total === 0) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        {emptyMessage}
      </p>
    );
  }

  const arcs = slices.reduce<
    { label: string; count: number; percentage: number; color: string; dashArray: string; dashOffset: number }[]
  >((acc, s, i) => {
    const priorOffset = acc.reduce((sum, a) => sum - a.dashOffset, 0);
    const fraction = s.count / total;
    const length = fraction * CIRCUMFERENCE;
    acc.push({
      label: s.label,
      count: s.count,
      percentage: (s.count / total) * 100,
      color: colors[i % colors.length],
      dashArray: `${length} ${CIRCUMFERENCE - length}`,
      dashOffset: -priorOffset,
    });
    return acc;
  }, []);

  const ariaLabel = `${ariaLabelPrefix}: ${slices
    .map((s) => `${s.label} -- ${s.count} (${((s.count / total) * 100).toFixed(1)}%)`)
    .join("; ")}. Total ${total}.`;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-6">
        <div role="img" aria-label={ariaLabel} className="relative h-32 w-32 shrink-0">
          <svg viewBox="0 0 120 120" className="h-32 w-32 -rotate-90">
            {arcs.map((arc) => (
              <circle
                key={arc.label}
                cx="60"
                cy="60"
                r={RADIUS}
                fill="none"
                stroke={arc.color}
                strokeWidth={STROKE_WIDTH}
                strokeDasharray={arc.dashArray}
                strokeDashoffset={arc.dashOffset}
              >
                <title>{`${arc.label}: ${arc.count} (${arc.percentage.toFixed(1)}%)`}</title>
              </circle>
            ))}
          </svg>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
            <span className="text-lg font-bold tabular-nums text-[var(--foreground)]">{total}</span>
            <span className="text-[10px] text-[var(--muted-foreground)]">{centerCaption}</span>
          </div>
        </div>

        <ul className="flex flex-col gap-1.5 text-sm">
          {arcs.map((arc) => (
            <li key={arc.label} className="flex items-center gap-2">
              <span aria-hidden="true" className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: arc.color }} />
              <span className="font-semibold text-[var(--foreground)]">{arc.label}</span>
              <span className="text-[var(--muted-foreground)]">
                {arc.count} ({arc.percentage.toFixed(1)}%)
              </span>
            </li>
          ))}
        </ul>
      </div>

      <table className="sr-only">
        <caption>{ariaLabelPrefix} -- exact counts</caption>
        <thead>
          <tr>
            <th scope="col">Label</th>
            <th scope="col">Count</th>
            <th scope="col">Percentage</th>
          </tr>
        </thead>
        <tbody>
          {arcs.map((arc) => (
            <tr key={arc.label}>
              <td>{arc.label}</td>
              <td>{arc.count}</td>
              <td>{arc.percentage.toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
