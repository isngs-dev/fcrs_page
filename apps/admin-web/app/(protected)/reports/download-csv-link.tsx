/**
 * "Download CSV" link (D8/D10) -- points at this app's own
 * `/reports/csv/[report]` proxy route (lib/reports.ts's `reportCsvPath`),
 * which forwards the caller's session cookie to admin-api server-side and
 * streams the bytes back. See that route handler's header comment for why
 * a raw `<a href>` to admin-api directly cannot work.
 */
import type { ReportName } from "@/lib/reports";
import { reportCsvPath } from "@/lib/reports";

export function DownloadCsvLink({ report, query }: { report: ReportName; query: string }) {
  return (
    <a
      href={reportCsvPath(report, query)}
      className="inline-flex h-8 items-center rounded-[9px] border border-[#e7e7e2] px-3.5 text-sm font-semibold text-[#191a17] transition-colors hover:bg-[#f7f7f3] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
    >
      ↧ Download CSV
    </a>
  );
}
