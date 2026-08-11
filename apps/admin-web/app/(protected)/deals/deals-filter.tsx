/**
 * Deal stage filter (SR-18 scope item 5). Plain GET `<form>`, mirrors
 * `leads/leads-filter.tsx`'s no-client-JS convention exactly -- no `select`
 * primitive dependency, submitting navigates to `/deals?stage=...`, which
 * resets pagination to page 1.
 */
import Link from "next/link";
import { OPPORTUNITIES_PIPELINE } from "@/lib/pipelines";
import { dealStageLabel } from "@/lib/deals-presentation";

const ALL_STAGES = [...OPPORTUNITIES_PIPELINE.stageOrder, ...OPPORTUNITIES_PIPELINE.terminalStages].filter(
  (stage, index, arr) => arr.indexOf(stage) === index
);

export function DealsFilter({
  currentStage,
  basePath = "/deals",
}: {
  currentStage: string | undefined;
  basePath?: string;
}) {
  return (
    <form action={basePath} method="get" className="flex flex-wrap items-center gap-2.5">
      <label htmlFor="deal-stage" className="sr-only">
        Stage
      </label>
      <select
        id="deal-stage"
        name="stage"
        defaultValue={currentStage ?? ""}
        className="min-h-9 rounded-[9px] border border-border bg-card px-3 text-[12.5px] text-[var(--ink-2)] outline-none focus-visible:border-ring"
      >
        <option value="">All stages</option>
        {ALL_STAGES.map((stage) => (
          <option key={stage} value={stage}>
            {dealStageLabel(stage)}
          </option>
        ))}
      </select>
      <button
        type="submit"
        className="min-h-9 rounded-[9px] border border-border bg-card px-3.5 text-[12.5px] font-semibold text-[var(--ink-2)] hover:bg-secondary"
      >
        Filter
      </button>
      {currentStage ? (
        <Link href={basePath} className="text-[12.5px] text-muted-foreground underline underline-offset-2">
          Clear filter
        </Link>
      ) : null}
    </form>
  );
}
