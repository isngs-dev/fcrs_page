/**
 * Stage filter (S13.4 decision 5). A plain GET `<form>` with a native
 * `<select name="stage">` -- no client JS, no `select` primitive dependency
 * (decision 7). Submitting navigates to `/leads?stage=...`, which resets
 * pagination to page 1 (the `page` param is simply omitted from this form,
 * so a fresh submit always lands on page 1). Server component -- no
 * `"use client"` needed.
 */
import Link from "next/link";
import { LEAD_STAGES } from "@/lib/leads";

const STAGE_LABELS: Record<(typeof LEAD_STAGES)[number], string> = {
  captured: "Captured",
  qualified: "Qualified",
  contacted: "Contacted",
  converted: "Converted",
  disqualified: "Disqualified",
};

/**
 * `basePath` (S13.7): the per-client leads screen passes
 * `/clients/{tenantId}/leads` so the filter form and "Clear filter" link
 * stay on that same tenant-scoped route instead of the implicit `/leads`.
 * Defaults to `/leads`, preserving the existing CLIENT_ADMIN/AGENT behavior.
 *
 * SR-24: trigger/button chrome restyled to the reference's `.btn-outline`
 * geometry (Console.dc.html:43) -- the underlying native `<select>`-based GET
 * form is unchanged (still the one real, working filter; see leads/SR-24
 * plan item 3/4 -- no fake dropdown, no per-column filter icons).
 */
export function LeadsFilter({
  currentStage,
  currentQ,
  currentParams,
  basePath = "/leads",
}: {
  currentStage: string | undefined;
  currentQ?: string;
  currentParams?: URLSearchParams;
  basePath?: string;
}) {
  const preservedParams = Array.from(currentParams ?? new URLSearchParams()).filter(
    ([key]) => !["page", "stage", "q", "lead", "tab"].includes(key)
  );
  const clearParams = new URLSearchParams(currentParams);
  clearParams.delete("page");
  clearParams.delete("stage");
  clearParams.delete("q");
  clearParams.delete("lead");
  clearParams.delete("tab");
  const clearQuery = clearParams.toString();
  const clearHref = clearQuery ? `${basePath}?${clearQuery}` : basePath;

  return (
    <form
      action={basePath}
      method="get"
      className="flex flex-wrap items-center gap-2.5"
    >
      {preservedParams.map(([key, value]) => (
        <input key={`${key}-${value}`} type="hidden" name={key} value={value} />
      ))}
      <label htmlFor="stage" className="sr-only">
        Stage
      </label>
      <div className="relative">
        <select
          id="stage"
          name="stage"
          defaultValue={currentStage ?? ""}
          className="h-[38px] appearance-none rounded-[10px] border border-border bg-card py-0 pr-8 pl-4 text-[13.5px] font-semibold text-[var(--ink-2)] outline-none focus-visible:border-ring"
        >
          <option value="">All stages</option>
          {LEAD_STAGES.map((stage) => (
            <option key={stage} value={stage}>
              {STAGE_LABELS[stage]}
            </option>
          ))}
        </select>
        <svg
          aria-hidden="true"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-[var(--ink-2)]"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </div>
      <label htmlFor="lead-search" className="sr-only">
        Search name or email
      </label>
      <input
        id="lead-search"
        name="q"
        type="search"
        minLength={2}
        maxLength={200}
        defaultValue={currentQ ?? ""}
        placeholder="Search name or email"
        className="h-[38px] w-48 rounded-[10px] border border-border bg-card px-3 text-[13.5px] text-[var(--ink-2)] outline-none placeholder:text-muted-foreground focus-visible:border-ring"
      />
      <button
        type="submit"
        className="inline-flex h-[38px] items-center gap-2 rounded-[10px] border border-border bg-card px-4 text-[13.5px] font-semibold text-[var(--ink-2)] hover:bg-[#e6e6e6]"
      >
        <svg
          aria-hidden="true"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M4 6h16M7 12h10M10 18h4" />
        </svg>
        Filter
      </button>
      {currentStage || currentQ ? (
        <Link href={clearHref} className="text-[12.5px] text-muted-foreground underline underline-offset-2">
          Clear filter
        </Link>
      ) : null}
    </form>
  );
}
