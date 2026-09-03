/**
 * Per-client leads-by-stage report (platform-admin console). Mirrors
 * `clients/[tenantId]/reports/funnel/page.tsx`'s exact tenant-scoping
 * pattern -- see that file's header comment.
 */
import Link from "next/link";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import { getLeadsByStageReport, resolveReportQuery } from "@/lib/reports";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { StageBars } from "@/app/(protected)/reports/leads-by-stage/stage-bars";

interface PageProps {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

export default async function ClientLeadsByStagePage({ params, searchParams }: PageProps) {
  const { tenantId } = await params;
  const basePath = `/clients/${tenantId}/reports/leads-by-stage`;

  const resolvedSearchParams = await searchParams;
  const rawRange = firstValue(resolvedSearchParams.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;

  const result = await getLeadsByStageReport({ range }, tenantId);
  const csvQuery = resolveReportQuery({ range });

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <Link
        href={`/clients/${tenantId}/reports`}
        className="text-sm text-[var(--muted-foreground)] hover:underline"
      >
        ← Back to reports
      </Link>

      <div className="flex flex-wrap items-center gap-3.5">
        <h1 className="text-xl font-bold text-[var(--foreground)]">Leads by stage</h1>
        <div className="ml-auto flex items-center gap-3">
          <DownloadCsvLink report="leads-by-stage" query={csvQuery} tenantId={tenantId} />
          <ReportRange basePath={basePath} currentRange={range} />
        </div>
      </div>

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
        <div className="flex flex-col gap-4 rounded-[14px] border border-[var(--border)] p-5">
          <div className="flex items-baseline gap-2.5">
            <span className="text-sm font-bold text-[var(--foreground)]">
              {result.data.total.toLocaleString()} leads in this window
            </span>
          </div>
          <StageBars data={result.data} />
        </div>
      )}
    </div>
  );
}
