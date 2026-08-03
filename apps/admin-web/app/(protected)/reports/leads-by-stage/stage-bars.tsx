/**
 * Leads-by-stage pill-bars, mirroring `analytics/funnel-bars.tsx`'s shape
 * (D10): proportional pill width + exact count as text, `role="img"` +
 * full `aria-label`, a visually-hidden table of exact values.
 */
import type { LeadsByStageReport } from "@/lib/reports";

const STAGE_LABELS: Record<string, string> = {
  captured: "Captured",
  qualified: "Qualified",
  contacted: "Contacted",
  converted: "Converted",
  disqualified: "Disqualified",
};

const STAGE_ORDER = ["captured", "qualified", "contacted", "converted", "disqualified"] as const;
const STAGE_COLORS = ["#191a17", "#45463f", "#70716a", "#e4f222", "#c2452d"] as const;

export function StageBars({ data }: { data: LeadsByStageReport }) {
  if (data.total === 0) {
    return (
      <p role="status" className="text-sm text-[#70716a]">
        No data in this window.
      </p>
    );
  }

  const max = Math.max(...STAGE_ORDER.map((s) => data.stages[s] ?? 0), 1);

  return (
    <div className="flex flex-col gap-3">
      <div
        role="img"
        aria-label={`Leads by stage: ${STAGE_ORDER.map(
          (s) => `${STAGE_LABELS[s]} -- ${data.stages[s] ?? 0}`
        ).join("; ")}. Total ${data.total}.`}
        className="flex flex-col gap-2"
      >
        {STAGE_ORDER.map((stage, i) => {
          const count = data.stages[stage] ?? 0;
          const pct = Math.max((count / max) * 100, count > 0 ? 4 : 1);
          return (
            <div key={stage} className="flex items-center gap-3">
              <span className="w-24 shrink-0 text-xs font-semibold text-[#70716a]">
                {STAGE_LABELS[stage]}
              </span>
              <div className="h-6 flex-1 overflow-hidden rounded-[6px] bg-[#f7f7f3]">
                <div
                  title={`${STAGE_LABELS[stage]}: ${count}`}
                  className="h-full rounded-[6px]"
                  style={{ width: `${pct}%`, backgroundColor: STAGE_COLORS[i] }}
                />
              </div>
              <span className="w-10 shrink-0 text-right text-sm font-bold tabular-nums text-[#191a17]">
                {count}
              </span>
            </div>
          );
        })}
      </div>

      <table className="sr-only">
        <caption>Leads by stage -- exact counts</caption>
        <thead>
          <tr>
            <th scope="col">Stage</th>
            <th scope="col">Count</th>
          </tr>
        </thead>
        <tbody>
          {STAGE_ORDER.map((stage) => (
            <tr key={stage}>
              <td>{STAGE_LABELS[stage]}</td>
              <td>{data.stages[stage] ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
