/**
 * Reports dashboard (SR-22 rebuild of the SR-9.5 index). Matches the
 * reference's Reports/CRM artboard (Console.dc.html): 5 KPI tiles -> pipeline
 * bars + lead-sources donut -> score histogram -> agent-performance table ->
 * recent-conversions table, all on one page instead of a link grid.
 *
 * Every number here reuses SR-19's existing report fetchers (`lib/reports.ts`)
 * -- no new backend calls invented for this rebuild. The reference's
 * "New vs. qualified leads" line chart and "Lead score distribution" bars are
 * both represented (score histogram is the real one; the line-chart slot is
 * covered by the funnel steps, since this backend has no weekly new/qualified
 * time series endpoint -- inventing one is out of scope for a frontend
 * restyle sprint, see SR-22 spec Constraints).
 *
 * CLIENT_ADMIN + CLIENT_AGENT only (D7 -- full symmetric read access). The
 * eight individual report routes (`/reports/leads-by-stage` etc.) remain as
 * deep-link targets with their own CSV export + full detail; this page is
 * the at-a-glance summary the reference specifies as the primary /reports
 * surface.
 *
 * SR-27 slice 6: `ScoreHistogram` flipped from horizontal bars to vertical
 * columns (geometry only, data logic unchanged). The `delta` prop on
 * `KpiTile` (the "▲12%" percent-change badges) is DELETED, not just
 * unused -- `analytics/reports_routes.py` has no endpoint returning a
 * prior-window figure (G11) and a prop that exists only to someday render an
 * unbacked indicator is a latent dead control.
 *
 * SR-30 (D30-10, re-verifying SR-27's own record): SR-27 section 6.2 R2
 * claimed there is no deal-value aggregate anywhere in `reports_routes.py`.
 * A fresh re-read at SR-30 planning time found that claim STALE --
 * `WinLossOutcomeResponse` carries `avg_deal_size`/`amount_total`/
 * `amount_null_count` and `WinLossReportResponse` carries `currency`/
 * `currency_configured` (`reports_routes.py:692-712`), and the frontend
 * already fetches and maps all of it (`lib/reports.ts:280-301`). "Avg deal"
 * is now a REAL fifth KPI tile built from that field. "Win rate" is KEPT,
 * not replaced -- it is the only conversion-quality number on this page.
 * G11 (period-over-period deltas) and G12 (weekly new-vs-qualified time
 * series) were both re-verified as still genuinely open at SR-30 planning
 * time (fresh grep across `services/api/src/api/analytics/` finds no
 * comparison logic and no lead-state time-series route) -- the dual-line
 * chart substitution (funnel steps standing in) and the absence of %-change
 * badges both stand, unchanged.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import {
  getLeadsByStageReport,
  getWinLossReport,
  getFunnelReport,
  getLeadSourcesReport,
  getScoreDistributionReport,
  getAgentPerformanceReport,
  getRecentConversionsReport,
  resolveReportQuery,
} from "@/lib/reports";
import { listMembers } from "@/lib/members";
import { SoftCard } from "@/components/admin/soft-card";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { FunnelSteps } from "@/app/(protected)/reports/funnel/funnel-steps";
import { LeadSourcesDonut } from "@/app/(protected)/reports/lead-sources/lead-sources-donut";
import { ScoreHistogram } from "@/app/(protected)/reports/score-distribution/score-histogram";
import { AgentPerformanceTable } from "@/app/(protected)/reports/agent-performance/agent-performance-table";
import { RecentConversionsTable } from "@/app/(protected)/reports/recent-conversions/recent-conversions-table";
import { formatRate } from "@/lib/analytics";
import { formatDealSize } from "@/app/(protected)/reports/reports-kpi-presentation";

interface PageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

const DEEP_LINKS = [
  { href: "/reports/leads-by-stage", title: "Leads by stage" },
  { href: "/reports/bookings", title: "Bookings" },
  { href: "/reports/funnel", title: "Conversion funnel" },
  { href: "/reports/win-loss", title: "Win / loss" },
  { href: "/reports/lead-sources", title: "Lead sources" },
  { href: "/reports/score-distribution", title: "Lead score distribution" },
  { href: "/reports/agent-performance", title: "Agent performance" },
  { href: "/reports/recent-conversions", title: "Recent conversions" },
  { href: "/reports/roi", title: "ROI dashboard" },
] as const;

function KpiTile({ label, value }: { label: string; value: string }) {
  return (
    <SoftCard className="p-4">
      <div className="text-[11px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">
        {label}
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <div className="text-[30px] font-bold text-foreground">{value}</div>
      </div>
    </SoftCard>
  );
}

function ErrorNote({ message, correlationId }: { message: string; correlationId: string }) {
  return (
    <p role="alert" className="rounded-[14px] border border-[var(--danger-fg)]/40 bg-[#f6e3df]/60 p-4 text-sm text-[var(--danger-fg)]">
      {message}
      {correlationId ? (
        <span className="block text-xs text-[var(--danger-fg)]/80">Correlation ID: {correlationId}</span>
      ) : null}
    </p>
  );
}

export default async function ReportsIndexPage({ searchParams }: PageProps) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawRange = firstValue(params.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;

  const [stageResult, winLossResult, funnelResult, sourcesResult, scoreResult, agentResult, recentResult, membersResult] =
    await Promise.all([
      getLeadsByStageReport({ range }),
      getWinLossReport({ range }),
      getFunnelReport({ range }),
      getLeadSourcesReport({ range }),
      getScoreDistributionReport({ range }),
      getAgentPerformanceReport({ range }),
      getRecentConversionsReport({ range }),
      listMembers(),
    ]);
  const csvQuery = resolveReportQuery({ range });

  const agentNames: Record<string, string> =
    membersResult.status === "ok"
      ? Object.fromEntries(
          membersResult.items
            .filter((m): m is typeof m & { name: string } => m.name !== null)
            .map((m) => [m.id, m.name])
        )
      : {};

  const totalLeads = stageResult.status === "ok" ? stageResult.data.total : null;
  const qualified = stageResult.status === "ok" ? (stageResult.data.stages["qualified"] ?? 0) : null;
  const won = winLossResult.status === "ok" ? winLossResult.data.won.count : null;
  const winRate = winLossResult.status === "ok" ? winLossResult.data.winRate : null;
  const avgDealSize = winLossResult.status === "ok" ? winLossResult.data.won.avgDealSize : null;
  const currency = winLossResult.status === "ok" ? winLossResult.data.currency : "USD";
  const currencyConfigured = winLossResult.status === "ok" ? winLossResult.data.currencyConfigured : false;

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <div className="flex flex-wrap items-end justify-between gap-3.5">
        <div>
          <Link href="/" className="text-sm text-muted-foreground hover:underline">
            ← Back to console
          </Link>
          <h1 className="mt-2 text-[28px] font-semibold text-foreground">Reports</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Pipeline, sources, and team performance across your CRM in this window.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <DownloadCsvLink report="leads-by-stage" query={csvQuery} />
          <ReportRange basePath="/reports" currentRange={range} />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-5">
        <KpiTile label="Total leads" value={totalLeads === null ? "—" : totalLeads.toLocaleString()} />
        <KpiTile label="Qualified" value={qualified === null ? "—" : qualified.toLocaleString()} />
        <KpiTile label="Won" value={won === null ? "—" : won.toLocaleString()} />
        <KpiTile label="Win rate" value={winRate === null ? "No data" : formatRate(winRate)} />
        <KpiTile label="Avg deal" value={formatDealSize(avgDealSize, currency, currencyConfigured)} />
      </div>

      <div className="flex flex-col gap-4 lg:flex-row lg:items-stretch">
        <SoftCard className="flex-1 p-5">
          <h2 className="mb-4 text-[15px] font-bold text-foreground">Lead pipeline</h2>
          {funnelResult.status === "error" ? (
            <ErrorNote message={funnelResult.message} correlationId={funnelResult.correlationId} />
          ) : (
            <FunnelSteps data={funnelResult.data} />
          )}
        </SoftCard>
        <SoftCard className="p-5 lg:w-[340px] lg:flex-none">
          <h2 className="mb-4 text-[15px] font-bold text-foreground">Lead sources</h2>
          {sourcesResult.status === "error" ? (
            <ErrorNote message={sourcesResult.message} correlationId={sourcesResult.correlationId} />
          ) : (
            <LeadSourcesDonut data={sourcesResult.data} />
          )}
        </SoftCard>
      </div>

      <SoftCard className="p-5">
        <h2 className="mb-1 text-[15px] font-bold text-foreground">Lead score distribution</h2>
        <p className="mb-4 text-xs text-muted-foreground">Count of leads by score band in this window.</p>
        {scoreResult.status === "error" ? (
          <ErrorNote message={scoreResult.message} correlationId={scoreResult.correlationId} />
        ) : (
          <ScoreHistogram data={scoreResult.data} />
        )}
      </SoftCard>

      <SoftCard className="overflow-hidden p-0">
        <h2 className="border-b border-[var(--row-line)] px-5 py-3.5 text-[15px] font-bold text-foreground">
          Agent performance
        </h2>
        <div className="p-5">
          {agentResult.status === "error" ? (
            <ErrorNote message={agentResult.message} correlationId={agentResult.correlationId} />
          ) : (
            <AgentPerformanceTable data={agentResult.data} agentNames={agentNames} />
          )}
        </div>
      </SoftCard>

      <SoftCard className="overflow-hidden p-0">
        <h2 className="border-b border-[var(--row-line)] px-5 py-3.5 text-[15px] font-bold text-foreground">
          Recent conversions
        </h2>
        <div className="p-5">
          {recentResult.status === "error" ? (
            <ErrorNote message={recentResult.message} correlationId={recentResult.correlationId} />
          ) : (
            <RecentConversionsTable data={recentResult.data} />
          )}
        </div>
      </SoftCard>

      <div className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase">
          Full reports
        </h2>
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
          {DEEP_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="rounded-[10px] border border-border px-3.5 py-2.5 text-[13px] font-semibold text-[var(--ink-2)] transition-colors hover:border-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-foreground"
            >
              {link.title} →
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
