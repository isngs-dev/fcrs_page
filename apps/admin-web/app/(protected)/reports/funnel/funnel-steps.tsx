/**
 * Conversion funnel steps (SR-9.5 D6/D10) -- mirrors
 * `analytics/funnel-bars.tsx`'s pill shape. `disqualified` is rendered as a
 * clearly separate off-ramp card, never as a fifth step.
 *
 * SR-30 D30-11: the inline per-step label now shows the CONVERSION rate
 * (1 - dropOffRate) rather than the drop-off rate, matching the reference
 * screenshot. The two are real complements of the same already-fetched
 * `dropOffRate` field (`reports_routes.py:521-524`) -- no new data. Guarded:
 * a null `dropOffRate` (the first step, or a zero-denominator step) renders
 * nothing inline, never a fabricated "100%". The raw drop-off rate is kept,
 * unchanged, in the sr-only table below so no information is lost.
 */
import { formatRate } from "@/lib/analytics";
import type { FunnelReport } from "@/lib/reports";

const STAGE_LABELS: Record<string, string> = {
  captured: "Captured",
  qualified: "Qualified",
  contacted: "Contacted",
  converted: "Converted",
};

// SR-15 D1: the final "converted" step's citron fill is deleted and
// re-decided to the design's success-fg green, mirroring
// `reports/leads-by-stage/stage-bars.tsx`'s identical re-decision.
const STAGE_COLORS = ["var(--foreground)", "var(--ink-2)", "#8a8b82", "#3f7d57"] as const;

export function FunnelSteps({ data }: { data: FunnelReport }) {
  const allZero = data.steps.every((s) => s.count === 0) && data.disqualifiedCount === 0;
  if (allZero) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        No data in this window.
      </p>
    );
  }

  const max = Math.max(...data.steps.map((s) => s.count), 1);

  return (
    <div className="flex flex-col gap-4">
      <div
        role="img"
        aria-label={`Conversion funnel: ${data.steps
          .map(
            (s) =>
              `${STAGE_LABELS[s.stage] ?? s.stage} -- ${s.count}${
                s.dropOffRate === null ? "" : `, ${formatRate(s.dropOffRate)} drop-off`
              }`
          )
          .join("; ")}. Overall conversion rate: ${formatRate(data.overallConversionRate)}.`}
        className="flex flex-col gap-2"
      >
        {data.steps.map((step, i) => {
          const pct = Math.max((step.count / max) * 100, step.count > 0 ? 6 : 2);
          const widthPx = Math.round((pct / 100) * 260);
          return (
            <div key={step.stage} className="flex items-center gap-3 text-sm">
              <span
                aria-hidden
                className="h-3 rounded-full"
                style={{ width: `${Math.max(widthPx, 12)}px`, backgroundColor: STAGE_COLORS[i] }}
              />
              <span className="flex-1">
                <span className="font-bold text-[var(--foreground)] tabular-nums">
                  {step.count.toLocaleString()}
                </span>{" "}
                <span className="text-[var(--ink-2)]">{STAGE_LABELS[step.stage] ?? step.stage}</span>
              </span>
              <span className="text-xs text-[var(--muted-foreground)]">
                {step.dropOffRate === null ? "" : `→ ${formatRate(1 - step.dropOffRate)} conversion`}
              </span>
            </div>
          );
        })}
      </div>

      <div className="flex items-center justify-between rounded-[10px] border border-dashed border-[#d5d5cb] bg-[var(--secondary)] px-4 py-2.5 text-sm">
        <span className="text-[var(--muted-foreground)]">Disqualified (off-ramp, not a funnel step)</span>
        <span className="font-bold text-[var(--foreground)] tabular-nums">
          {data.disqualifiedCount.toLocaleString()}
        </span>
      </div>

      <div className="flex items-center justify-between text-sm">
        <span className="text-[var(--muted-foreground)]">Overall conversion rate (captured → converted)</span>
        <span className="font-bold text-[var(--foreground)]">{formatRate(data.overallConversionRate)}</span>
      </div>

      <table className="sr-only">
        <caption>Conversion funnel -- exact counts and drop-off rates</caption>
        <thead>
          <tr>
            <th scope="col">Stage</th>
            <th scope="col">Count</th>
            <th scope="col">Drop-off rate</th>
          </tr>
        </thead>
        <tbody>
          {data.steps.map((step) => (
            <tr key={step.stage}>
              <td>{STAGE_LABELS[step.stage] ?? step.stage}</td>
              <td>{step.count}</td>
              <td>{step.dropOffRate === null ? "No data" : formatRate(step.dropOffRate)}</td>
            </tr>
          ))}
          <tr>
            <td>Disqualified (off-ramp)</td>
            <td>{data.disqualifiedCount}</td>
            <td>--</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
