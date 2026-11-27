/**
 * Every outreach page renders a bare `flex flex-col gap-5` column; this gives the whole
 * section the console's standard page spacing (owner, 2026-11-27: titles sat flush
 * against the top-left corner).
 */
export default function OutreachLayout({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-1 flex-col px-4 py-8 sm:px-6 lg:px-8 lg:py-9">{children}</div>;
}
