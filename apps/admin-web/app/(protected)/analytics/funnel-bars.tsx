/**
 * Funnel pill-bars (5b: "funnel pill-bars"), restyled to the reference design.
 *
 * 5b's mock funnel is "conversations -> engaged 3+ turns -> leads -> calls
 * booked" -- this backend's analytics overview has no "engaged 3+ turns"
 * or "leads" aggregate (leads live in the separate `lead-capture-crm`
 * module / `lib/leads.ts`, out of scope for this restyle). Rather than
 * fabricate those two stages, this funnel uses the 3 stages this endpoint
 * actually computes end-to-end:
 *   1. Conversations           -> totals.conversations
 *   2. Schedule CTA shown      -> schedule.ctaConversations
 *   3. Calls booked            -> schedule.conversions
 * All three are real counts from `AnalyticsOverview`. Each stage shows its
 * exact count as text (not just a bar width) and pill width is
 * proportional to the largest stage, floored so a non-zero stage is never
 * visually invisible.
 */
import type { AnalyticsOverview } from "@/lib/analytics";

// SR-15 D1: the funnel's third-stage citron fill is deleted and re-decided
// to a lighter monochrome grey, distinguishable from the other two stages
// by value alone.
const STAGE_COLORS = ["var(--foreground)", "var(--ink-2)", "var(--muted-foreground)"] as const;

export function FunnelBars({ data }: { data: AnalyticsOverview }) {
  const stages = [
    { label: "conversations", value: data.totals.conversations },
    { label: "shown a scheduling CTA", value: data.schedule.ctaConversations },
    { label: "calls booked", value: data.schedule.conversions },
  ];
  const max = Math.max(...stages.map((s) => s.value), 1);

  return (
    <div className="flex flex-col gap-2 border-t border-[var(--secondary)] pt-3.5">
      <span className="text-[11.5px] font-semibold text-[var(--muted-foreground)]">FUNNEL</span>
      <ul className="flex flex-col gap-1.5">
        {stages.map((stage, i) => {
          const pct = Math.max((stage.value / max) * 100, stage.value > 0 ? 6 : 2);
          const widthPx = Math.round((pct / 100) * 130);
          return (
            <li key={stage.label} className="flex items-center gap-2 text-xs text-[var(--ink-2)]">
              <span
                aria-hidden
                className="h-2.5 rounded-full"
                style={{ width: `${Math.max(widthPx, 10)}px`, backgroundColor: STAGE_COLORS[i] }}
              />
              <span>
                <span className="font-semibold text-[var(--foreground)] tabular-nums">
                  {stage.value.toLocaleString()}
                </span>{" "}
                {stage.label}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
