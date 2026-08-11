/**
 * Deals Table/Board segmented toggle (SR-18 D1: "reuses the Leads page's
 * shape exactly"). Mirrors `leads/leads-view-toggle.tsx`'s `?view=`
 * URL-driven convention, unlike leads there is no placeholder history here
 * -- Deals ships with a working board from day one.
 */
import Link from "next/link";

export function DealsViewToggle({
  view,
  basePath,
  currentParams,
}: {
  view: "table" | "board";
  basePath: string;
  currentParams: URLSearchParams;
}) {
  function hrefFor(nextView: "table" | "board"): string {
    const params = new URLSearchParams(currentParams);
    params.delete("deal");
    if (nextView === "table") {
      params.delete("view");
    } else {
      params.set("view", nextView);
    }
    const qs = params.toString();
    return qs ? `${basePath}?${qs}` : basePath;
  }

  return (
    <div className="flex overflow-hidden rounded-lg border border-border text-xs font-semibold" role="group" aria-label="Deals view">
      <Link
        href={hrefFor("board")}
        scroll={false}
        aria-current={view === "board" ? "page" : undefined}
        className="min-h-9 px-3.5 py-1.5"
        style={view === "board" ? { background: "#333333", color: "#fff" } : { color: "var(--ink-2)" }}
      >
        Board
      </Link>
      <Link
        href={hrefFor("table")}
        scroll={false}
        aria-current={view === "table" ? "page" : undefined}
        className="min-h-9 px-3.5 py-1.5"
        style={view === "table" ? { background: "#333333", color: "#fff" } : { color: "var(--ink-2)" }}
      >
        Table
      </Link>
    </div>
  );
}
