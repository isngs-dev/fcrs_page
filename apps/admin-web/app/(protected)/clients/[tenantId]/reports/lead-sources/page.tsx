/**
 * Per-client lead-sources report (platform-admin console). Mirrors
 * `clients/[tenantId]/reports/funnel/page.tsx`'s exact tenant-scoping
 * pattern -- see that file's header comment.
 */
import Link from "next/link";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import { getLeadSourcesReport, resolveReportQuery } from "@/lib/reports";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { LeadSourcesDonut } from "@/app/(protected)/reports/lead-sources/lead-sources-donut";

interface PageProps {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

export default async function ClientLeadSourcesPage({ params, searchParams }: PageProps) {
  const { tenantId } = await params;
  const basePath = `/clients/${tenantId}/reports/lead-sources`;

  const resolvedSearchParams = await searchParams;
  const rawRange = firstValue(resolvedSearchParams.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;

  const result = await getLeadSourcesReport({ range }, tenantId);
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
        <h1 className="text-xl font-bold text-[var(--foreground)]">Lead sources</h1>
        <div className="ml-auto flex items-center gap-3">
          <DownloadCsvLink report="lead-sources" query={csvQuery} tenantId={tenantId} />
          <ReportRange basePath={basePath} currentRange={range} />
        </div>
      </div>

      <p className="text-[13px] text-[var(--muted-foreground)]">
        Where leads in this window came from. Shows whatever real sources exist -- never padded to
        match a fixed set of categories.
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
          <LeadSourcesDonut data={result.data} />
        </div>
      )}
    </div>
  );
}
