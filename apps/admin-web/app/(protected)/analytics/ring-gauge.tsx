/**
 * Ring gauge (SR-27 slice 9, per SR-23's spec re-confirmed): r=52,
 * stroke-width 13, round line cap, light-grey track, centered percentage.
 * Used for `deflection_rate` and `grounded_rate` (both real fields,
 * `analytics/routes.py:88-89`), replacing the plain `StatCard` number tiles
 * in `analytics-cards.tsx`.
 *
 * Explicit no-data state (CLAUDE.md §3 no-silent-fallback): a `null` rate
 * renders "No data" text with an EMPTY track, never a 0%-filled ring --
 * a 0% ring would read as a real measured zero, which a null value is not.
 */
const RADIUS = 52;
const STROKE_WIDTH = 13;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

export function RingGauge({
  label,
  rate,
  caption,
}: {
  label: string;
  /** 0..1, or null when there's no data to compute a rate from. */
  rate: number | null;
  caption: string;
}) {
  const pct = rate === null ? 0 : Math.max(0, Math.min(1, rate)) * 100;
  const dashOffset = CIRCUMFERENCE * (1 - pct / 100);

  return (
    <div className="flex flex-col items-center gap-2 rounded-[14px] border border-border bg-card p-4 shadow-[0_1px_2px_rgba(28,27,25,.03)]">
      <span className="self-start text-[11.5px] font-semibold text-muted-foreground uppercase">{label}</span>
      <div className="relative h-[124px] w-[124px]">
        <svg viewBox="0 0 124 124" className="h-full w-full -rotate-90">
          <circle cx="62" cy="62" r={RADIUS} fill="none" stroke="var(--secondary)" strokeWidth={STROKE_WIDTH} />
          {rate !== null ? (
            <circle
              cx="62"
              cy="62"
              r={RADIUS}
              fill="none"
              stroke="var(--foreground)"
              strokeWidth={STROKE_WIDTH}
              strokeLinecap="round"
              strokeDasharray={CIRCUMFERENCE}
              strokeDashoffset={dashOffset}
            />
          ) : null}
        </svg>
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          {rate === null ? (
            <span className="text-[13px] font-semibold text-muted-foreground">No data</span>
          ) : (
            <span className="text-[22px] font-bold tabular-nums text-foreground">{Math.round(pct)}%</span>
          )}
        </div>
      </div>
      <span className="text-center text-[11.5px] font-semibold text-muted-foreground">{caption}</span>
    </div>
  );
}
