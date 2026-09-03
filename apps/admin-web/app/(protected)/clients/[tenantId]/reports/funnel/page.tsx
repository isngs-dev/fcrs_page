/**
 * Per-client conversion funnel report (platform-admin console). Reuses
 * `app/(protected)/reports/funnel/page.tsx`'s `FunnelSteps` + fetcher as-is,
 * parameterized by the route's `{tenantId}` (mirrors
 * `clients/[tenantId]/analytics/page.tsx`'s exact pattern) so
 * `getFunnelReport` targets the PLATFORM_ADMIN tenant-scoped surface
 * `/admin/tenants/{tenantId}/analytics/reports/funnel` instead of the
 * implicit `/admin/analytics/reports/funnel`. No page-level `requireRole` --
 * the parent `clients/[tenantId]/layout.tsx` already gates PLATFORM_ADMIN
 * for the whole subtree.
 */
import Link from "next/link";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import { getFunnelReport, resolveReportQuery } from "@/lib/reports";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { FunnelSteps } from "@/app/(protected)/reports/funnel/funnel-steps";

interface PageProps {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

export default async function ClientFunnelReportPage({ params, searchParams }: PageProps) {
  const { tenantId } = await params;
  const basePath = `/clients/${tenantId}/reports/funnel`;

  const resolvedSearchParams = await searchParams;
  const rawRange = firstValue(resolvedSearchParams.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;

  const result = await getFunnelReport({ range }, tenantId);
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
        <h1 className="text-xl font-bold text-[var(--foreground)]">Conversion funnel</h1>
        <div className="ml-auto flex items-center gap-3">
          <DownloadCsvLink report="funnel" query={csvQuery} tenantId={tenantId} />
          <ReportRange basePath={basePath} currentRange={range} />
        </div>
      </div>

      <p className="text-[13px] text-[var(--muted-foreground)]">
        Snapshot of leads at their current stage in this window -- not a cohort progression.
        A lead that moved captured → converted appears only in &ldquo;Converted&rdquo;.
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
          <FunnelSteps data={result.data} />
        </div>
      )}
    </div>
  );
}
