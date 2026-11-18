/**
 * Plan 2.5: the lead numbers split by lead source or by campaign (server-rendered, no state).
 */
import type { PerformanceSegment } from "@/lib/outreach";
import { percent } from "@/lib/outreach-format";
import { SoftCard } from "@/components/admin/soft-card";

function count(n: number, rate: number | null) {
  return `${n} (${percent(rate)})`;
}

export function SegmentTable({
  title,
  rows,
  label = (key: string) => key,
}: {
  title: string;
  rows: PerformanceSegment[];
  label?: (key: string) => string;
}) {
  return (
    <SoftCard className="px-5 py-4">
      <h2 className="pb-2 text-[16px] font-semibold">{title}</h2>
      {rows.length === 0 ? (
        <p className="text-[12.5px] text-muted-foreground">No leads in this period.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[13px]">
            <thead>
              <tr className="text-[12px] text-muted-foreground">
                <th className="py-1 pr-3 font-medium">{title.replace(/^By /, "").replace(/^\w/, (c) => c.toUpperCase())}</th>
                <th className="py-1 pr-3 font-medium">Leads</th>
                <th className="py-1 pr-3 font-medium">Meetings</th>
                <th className="py-1 pr-3 font-medium">Qualified</th>
                <th className="py-1 font-medium">Handed to your team</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key} className="border-t border-[var(--row-line)]">
                  <td className="py-1.5 pr-3 font-semibold">{label(r.key)}</td>
                  <td className="py-1.5 pr-3">{r.leads}</td>
                  <td className="py-1.5 pr-3">{count(r.meetings, r.leadToMeeting)}</td>
                  <td className="py-1.5 pr-3">{count(r.qualified, r.qualificationRate)}</td>
                  <td className="py-1.5">{count(r.handedOff, r.handoffRate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SoftCard>
  );
}
