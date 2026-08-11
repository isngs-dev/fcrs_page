/**
 * Bookings report page (SR-9.5 D5/D10). Bucket toggle includes Month (D3).
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import {
  getBookingsReport,
  resolveBookingsQuery,
  REPORT_BUCKETS,
  DEFAULT_REPORT_BUCKET,
  type ReportBucket,
} from "@/lib/reports";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { BookingsBars } from "@/app/(protected)/reports/bookings/bookings-bars";

interface PageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

export default async function BookingsReportPage({ searchParams }: PageProps) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawRange = firstValue(params.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;
  const rawBucket = firstValue(params.bucket);
  const bucket: ReportBucket =
    rawBucket && (REPORT_BUCKETS as readonly string[]).includes(rawBucket)
      ? (rawBucket as ReportBucket)
      : DEFAULT_REPORT_BUCKET;

  const result = await getBookingsReport({ range, bucket });
  const csvQuery = resolveBookingsQuery({ range, bucket });

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <Link href="/reports" className="text-sm text-[var(--muted-foreground)] hover:underline">
        ← Back to reports
      </Link>

      <div className="flex flex-wrap items-center gap-3.5">
        <h1 className="text-xl font-bold text-[var(--foreground)]">Bookings</h1>
        <div className="ml-auto flex items-center gap-3">
          <DownloadCsvLink report="bookings" query={csvQuery} />
          <ReportRange
            basePath="/reports/bookings"
            currentRange={range}
            currentBucket={bucket}
            showBucket
          />
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
          <div className="flex flex-wrap items-baseline gap-4 text-sm">
            <span className="font-bold text-[var(--foreground)]">
              {result.data.totals.totalExcludingCancelled.toLocaleString()} active bookings
            </span>
            <span className="text-[var(--muted-foreground)]">
              {result.data.totals.cancelled.toLocaleString()} cancelled
            </span>
          </div>
          <BookingsBars data={result.data} />
        </div>
      )}
    </div>
  );
}
