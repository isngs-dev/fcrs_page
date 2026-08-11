/**
 * Agent-performance report page (SR-19 D7/D10). Server-first: range state
 * lives in the URL, the server component fetches once per navigation.
 * Resolves agent names via ONE `listMembers()` call (D7 -- no SQL JOIN, no
 * N+1), joined in memory with the report's raw `assignedAgentId`s.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import { getAgentPerformanceReport, resolveReportQuery } from "@/lib/reports";
import { listMembers } from "@/lib/members";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { AgentPerformanceTable } from "@/app/(protected)/reports/agent-performance/agent-performance-table";

interface PageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

export default async function AgentPerformancePage({ searchParams }: PageProps) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawRange = firstValue(params.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;

  const [result, membersResult] = await Promise.all([
    getAgentPerformanceReport({ range }),
    listMembers(),
  ]);
  const csvQuery = resolveReportQuery({ range });

  // D7: one request, joined in memory -- never a SQL JOIN, never an N+1.
  // A member with no name falls back to nothing here; the table falls back
  // to the raw id itself (never "Unknown").
  const agentNames: Record<string, string> =
    membersResult.status === "ok"
      ? Object.fromEntries(
          membersResult.items
            .filter((m): m is typeof m & { name: string } => m.name !== null)
            .map((m) => [m.id, m.name])
        )
      : {};

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <Link href="/reports" className="text-sm text-[var(--muted-foreground)] hover:underline">
        ← Back to reports
      </Link>

      <div className="flex flex-wrap items-center gap-3.5">
        <h1 className="text-xl font-bold text-[var(--foreground)]">Agent performance</h1>
        <div className="ml-auto flex items-center gap-3">
          <DownloadCsvLink report="agent-performance" query={csvQuery} />
          <ReportRange basePath="/reports/agent-performance" currentRange={range} />
        </div>
      </div>

      <p className="text-[13px] text-[var(--muted-foreground)]">
        Assigned, contacted, and won leads per agent in this window, including an explicit
        Unassigned row.
      </p>

      {result.status === "error" ? (
        <p
          role="alert"
          className="rounded-[14px] border border-[var(--danger-fg)]/40 bg-[#f6e3df]/60 p-4 text-sm text-[var(--danger-fg)]"
        >
          {result.message}
          {result.correlationId ? (
            <span className="block text-xs text-[var(--danger-fg)]/80">
              Correlation ID: {result.correlationId}
            </span>
          ) : null}
        </p>
      ) : (
        <div className="rounded-[14px] border border-[var(--border)] p-5">
          <AgentPerformanceTable data={result.data} agentNames={agentNames} />
        </div>
      )}
    </div>
  );
}
