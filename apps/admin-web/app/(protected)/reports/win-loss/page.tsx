/**
 * Win/loss report page (SR-9.5 D13/D10). Full symmetric read access for
 * CLIENT_ADMIN + CLIENT_AGENT, including revenue (D7).
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { ANALYTICS_RANGES, DEFAULT_RANGE_KEY } from "@/lib/analytics";
import { getWinLossReport, resolveReportQuery } from "@/lib/reports";
import { ReportRange } from "@/app/(protected)/reports/report-range";
import { DownloadCsvLink } from "@/app/(protected)/reports/download-csv-link";
import { WinLossCards } from "@/app/(protected)/reports/win-loss/win-loss-cards";

interface PageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

const ALL_RANGE_KEYS: readonly string[] = ANALYTICS_RANGES.map((r) => r.key);

export default async function WinLossReportPage({ searchParams }: PageProps) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawRange = firstValue(params.range);
  const range = ALL_RANGE_KEYS.includes(rawRange ?? "") ? rawRange! : DEFAULT_RANGE_KEY;

  const result = await getWinLossReport({ range });
  const csvQuery = resolveReportQuery({ range });

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <Link href="/reports" className="text-sm text-[#70716a] hover:underline">
        ← Back to reports
      </Link>

      <div className="flex flex-wrap items-center gap-3.5">
        <h1 className="text-xl font-bold text-[#191a17]">Win / loss</h1>
        <div className="ml-auto flex items-center gap-3">
          <DownloadCsvLink report="win-loss" query={csvQuery} />
          <ReportRange basePath="/reports/win-loss" currentRange={range} />
        </div>
      </div>

      <p className="text-[13px] text-[#70716a]">
        Closed deals only -- outcomes are grouped by when they closed, not when they were
        created or expected to close.
      </p>

      {result.status === "error" ? (
        <p
          role="alert"
          className="rounded-[14px] border border-[#c2452d]/40 bg-[#f6e3df]/60 p-4 text-sm text-[#c2452d]"
        >
          {result.message}
          {result.correlationId ? (
            <span className="block text-xs text-[#c2452d]/80">
              Correlation ID: {result.correlationId}
            </span>
          ) : null}
        </p>
      ) : (
        <div className="rounded-[14px] border border-[#e7e7e2] p-5">
          <WinLossCards data={result.data} />
        </div>
      )}
    </div>
  );
}
