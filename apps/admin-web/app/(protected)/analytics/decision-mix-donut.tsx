/**
 * Decision-mix donut (SR-27 slice 9): a thin wrapper over the shared
 * `components/admin/donut.tsx` `Donut` primitive (genericized from
 * `reports/lead-sources/lead-sources-donut.tsx`, per the handoff's
 * preference for one shared implementation over two copies of the same arc
 * math), replacing `DistributionBars` for `decisionDistribution` -- a real
 * field (`analytics/routes.py:86`).
 */
import { Donut } from "@/components/admin/donut";

export function DecisionMixDonut({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);

  return (
    <div className="flex flex-col gap-3 rounded-[14px] border border-[var(--border)] p-[18px]">
      <span className="text-sm font-bold text-[var(--foreground)]">Decision mix</span>
      <Donut
        slices={entries.map(([label, count]) => ({ label, count }))}
        total={total}
        centerCaption="turns"
        ariaLabelPrefix="Decision mix"
        emptyMessage="No data for this window."
      />
    </div>
  );
}
