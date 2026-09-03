/**
 * Per-client ROI dashboard (platform-admin console). Mirrors
 * `clients/[tenantId]/reports/bookings/page.tsx`'s exact tenant-scoping
 * pattern -- see that file's header comment.
 */
import Link from "next/link";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import {
  getLeadsOverTimeReport,
  getBookingsReport,
  getLeadSourcesReport,
  resolveBookingsQuery,
  REPORT_BUCKETS,
  DEFAULT_REPORT_BUCKET,
  type ReportBucket,
} from "@/lib/reports";
import { SoftCard } from "@/components/admin/soft-card";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { RoiTrendChart } from "@/app/(protected)/reports/roi/roi-trend-chart";
import { LeadSourcesDonut } from "@/app/(protected)/reports/lead-sources/lead-sources-donut";

interface PageProps {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

function ErrorNote({ message, correlationId }: { message: string; correlationId: string }) {
  return (
    <p
      role="alert"
      className="rounded-[14px] border border-[var(--danger-fg)]/40 bg-[#f6e3df]/60 p-4 text-sm text-[var(--danger-fg)]"
    >
      {message}
      {correlationId ? (
        <span className="block text-xs text-[var(--danger-fg)]/80">Correlation ID: {correlationId}</span>
      ) : null}
    </p>
  );
}

export default async function ClientRoiDashboardPage({ params, searchParams }: PageProps) {
  const { tenantId } = await params;
  const basePath = `/clients/${tenantId}/reports/roi`;

  const resolvedSearchParams = await searchParams;
  const rawRange = firstValue(resolvedSearchParams.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;
  const rawBucket = firstValue(resolvedSearchParams.bucket);
  const bucket: ReportBucket =
    rawBucket && (REPORT_BUCKETS as readonly string[]).includes(rawBucket)
      ? (rawBucket as ReportBucket)
      : DEFAULT_REPORT_BUCKET;

  const [leadsResult, bookingsResult, sourcesResult] = await Promise.all([
    getLeadsOverTimeReport({ range, bucket }, tenantId),
    getBookingsReport({ range, bucket }, tenantId),
    getLeadSourcesReport({ range }, tenantId),
  ]);
  const csvQuery = resolveBookingsQuery({ range, bucket });

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <Link
        href={`/clients/${tenantId}/reports`}
        className="text-sm text-[var(--muted-foreground)] hover:underline"
      >
        ← Back to reports
      </Link>

      <div className="flex flex-wrap items-center gap-3.5">
        <h1 className="text-xl font-bold text-[var(--foreground)]">ROI dashboard</h1>
        <div className="ml-auto flex items-center gap-3">
          <DownloadCsvLink report="leads-over-time" query={csvQuery} tenantId={tenantId} />
          <ReportRange basePath={basePath} currentRange={range} currentBucket={bucket} showBucket />
        </div>
      </div>

      <p className="text-[13px] text-[var(--muted-foreground)]">
        Leads generated and appointments booked over time -- the chatbot&rsquo;s real business outcomes,
        not conversation counts.
      </p>

      <div className="flex flex-col gap-4 lg:flex-row lg:items-stretch">
        <SoftCard className="flex-1 p-5">
          <h2 className="mb-4 text-[15px] font-bold text-foreground">Leads &amp; bookings over time</h2>
          {leadsResult.status === "error" ? (
            <ErrorNote message={leadsResult.message} correlationId={leadsResult.correlationId} />
          ) : bookingsResult.status === "error" ? (
            <ErrorNote message={bookingsResult.message} correlationId={bookingsResult.correlationId} />
          ) : (
            <RoiTrendChart leads={leadsResult.data} bookings={bookingsResult.data} />
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
    </div>
  );
}
