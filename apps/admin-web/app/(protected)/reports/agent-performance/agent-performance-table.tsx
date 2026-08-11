/**
 * Agent-performance table (SR-19 D7). SR-15's `TableCard` primitive; a
 * win-rate bar + percentage; `null` win_rate renders "No data", NEVER "0%"
 * (an agent assigned nothing has an UNKNOWN win rate, not a 0% one). The
 * explicit Unassigned row is always rendered, never dropped, so the
 * table's counts sum to the tenant's total leads in the window.
 *
 * Name resolution (D7): `assignedAgentId` is resolved to a display name via
 * the caller's own `agentNames` map -- built ONCE from the existing "list
 * tenant users" request (`listMembers()`, `lib/members.ts`) and joined here
 * in memory. This is deliberately NOT a SQL JOIN (tenant-isolation risk,
 * SR-9.5 D11) and NOT one HTTP request per agent (an N+1, SR-17 D5). An id
 * with no matching name renders as the raw id itself, NEVER "Unknown" -- an
 * id is a fact an admin can act on; "Unknown" is not.
 *
 * A note surfaced here on purpose (D7): under SR-9.5 D7's symmetric read
 * (inherited by this report), a CLIENT_AGENT can see every other agent's
 * win rate. That is a deliberate, pre-existing product decision, not new
 * behaviour introduced by this report.
 */
import { formatRate } from "@/lib/analytics";
import type { AgentPerformanceReport, AgentPerformanceRow } from "@/lib/reports";
import { TableCard, TableCell, TableHeadCell, TableRow } from "@/components/admin/table-card";

function agentLabel(id: string | null, agentNames: Record<string, string>): string {
  if (id === null) return "Unassigned";
  return agentNames[id] ?? id;
}

function WinRateBar({ winRate }: { winRate: number | null }) {
  if (winRate === null) {
    return <span className="text-[13px] text-[var(--muted-foreground)]">No data</span>;
  }
  const pct = Math.max(Math.min(winRate * 100, 100), 0);
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-20 overflow-hidden rounded-full bg-[var(--secondary)]">
        <div className="h-full rounded-full bg-[#3f7d57]" style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[13px] font-semibold tabular-nums text-[var(--foreground)]">
        {formatRate(winRate)}
      </span>
    </div>
  );
}

function AgentRow({
  row,
  agentNames,
}: {
  row: AgentPerformanceRow;
  agentNames: Record<string, string>;
}) {
  return (
    <TableRow>
      <TableCell className="font-semibold text-[var(--foreground)]">
        {agentLabel(row.assignedAgentId, agentNames)}
      </TableCell>
      <TableCell className="tabular-nums">{row.assigned}</TableCell>
      <TableCell className="tabular-nums">{row.contacted}</TableCell>
      <TableCell className="tabular-nums">{row.won}</TableCell>
      <TableCell>
        <WinRateBar winRate={row.winRate} />
      </TableCell>
    </TableRow>
  );
}

export function AgentPerformanceTable({
  data,
  agentNames,
}: {
  data: AgentPerformanceReport;
  agentNames: Record<string, string>;
}) {
  const totalAssigned = data.agents.reduce((sum, a) => sum + a.assigned, 0) + data.unassigned.assigned;

  if (totalAssigned === 0) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        No leads in this window.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <TableCard>
        <thead>
          <tr>
            <TableHeadCell>Agent</TableHeadCell>
            <TableHeadCell>Assigned</TableHeadCell>
            <TableHeadCell>Contacted</TableHeadCell>
            <TableHeadCell>Won</TableHeadCell>
            <TableHeadCell>Win rate</TableHeadCell>
          </tr>
        </thead>
        <tbody>
          {data.agents.map((row) => (
            <AgentRow key={row.assignedAgentId} row={row} agentNames={agentNames} />
          ))}
          <AgentRow row={data.unassigned} agentNames={agentNames} />
        </tbody>
      </TableCard>
      <p className="text-[11.5px] text-[var(--muted-foreground)]">
        Agents with zero assigned leads show &ldquo;No data&rdquo; for win rate, never 0%. Every
        agent (including CLIENT_AGENT viewers) can see this table -- an intended, existing
        product decision.
      </p>
    </div>
  );
}
