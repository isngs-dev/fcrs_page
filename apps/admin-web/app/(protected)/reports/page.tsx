/**
 * Reports index (SR-9.5 D1/D10) -- lists the four fixed CRM reports this
 * sprint ships. CLIENT_ADMIN + CLIENT_AGENT only (D7 -- full symmetric
 * read access, including revenue/win-loss). No ad-hoc report builder (SR-9
 * D5 forbids it outright) -- this page is a plain, static list of exactly
 * four named reports, not a query surface.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";

const REPORTS = [
  {
    href: "/reports/leads-by-stage",
    title: "Leads by stage",
    description: "How many leads are sitting at each pipeline stage right now.",
  },
  {
    href: "/reports/bookings",
    title: "Bookings",
    description: "Booking activity over time, with cancellations shown -- never hidden.",
  },
  {
    href: "/reports/funnel",
    title: "Conversion funnel",
    description: "Where leads drop off between captured and converted.",
  },
  {
    href: "/reports/win-loss",
    title: "Win / loss",
    description: "Closed deals, revenue, average time to close, and raw loss reasons.",
  },
] as const;

export default async function ReportsIndexPage() {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] sm:p-[28px]">
      <Link href="/" className="text-sm text-[#70716a] hover:underline">
        ← Back to console
      </Link>

      <h1 className="text-xl font-bold text-[#191a17]">Reports</h1>
      <p className="text-sm text-[#70716a]">
        Four fixed reports over your leads, bookings, and deals. Each has a date range, a
        CSV export, and honest empty states -- never fabricated data.
      </p>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {REPORTS.map((report) => (
          <Link
            key={report.href}
            href={report.href}
            className="flex flex-col gap-1.5 rounded-[14px] border border-[#e7e7e2] p-5 transition-colors hover:border-[#191a17] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
          >
            <span className="text-base font-bold text-[#191a17]">{report.title}</span>
            <span className="text-[13px] text-[#70716a]">{report.description}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}
