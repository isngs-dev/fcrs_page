/**
 * 4b pagination (HANDOFF-SPEC.md §2 Tables: "pagination = bordered 8px-radius
 * page chips, active ink/white"). Prev/Next-only paging (decision 3 of
 * S13.4) doesn't give us real page numbers to enumerate, so this renders the
 * current page as the single active chip flanked by disabled-look ← / →
 * affordances that are only real links when a prior/next page exists --
 * matches the existing Prev/Next semantics exactly, just restyled.
 *
 * SR-24 item 17: now rendered by `leads/page.tsx` INSIDE the same bordered
 * `TableCard` as a footer row (`border-t`, no standalone card of its own),
 * with 32px-square `.btn-sm`-style controls: `.btn-outline` for inactive
 * prev/next, `.btn-dark`-equivalent for the active page number.
 */
import Link from "next/link";

const chipBase =
  "grid size-8 place-items-center rounded-[9px] text-[12.5px] font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

export function LeadsPagination({
  page,
  hasPrevious,
  hasNext,
  prevHref,
  nextHref,
  rangeLabel,
}: {
  page: number;
  hasPrevious: boolean;
  hasNext: boolean;
  prevHref: string;
  nextHref: string;
  rangeLabel: string;
}) {
  return (
    <div className="flex items-center border-t border-[var(--row-line)] bg-card px-3.5 py-2.5 text-[12.5px] text-muted-foreground">
      <span>{rangeLabel}</span>
      <div className="ml-auto flex gap-1.5">
        {hasPrevious ? (
          <Link
            href={prevHref}
            scroll={false}
            aria-label="Previous page"
            className={`${chipBase} border border-border bg-card text-[var(--ink-2)] hover:bg-[#e6e6e6]`}
          >
            ←
          </Link>
        ) : (
          <span aria-hidden className={`${chipBase} border border-border text-[var(--line-2)]`}>
            ←
          </span>
        )}
        <span aria-current="page" className={`${chipBase} bg-[#333333] text-[#fbfaf7]`}>
          {page}
        </span>
        {hasNext ? (
          <Link
            href={nextHref}
            scroll={false}
            aria-label="Next page"
            className={`${chipBase} border border-border bg-card text-[var(--ink-2)] hover:bg-[#e6e6e6]`}
          >
            →
          </Link>
        ) : (
          <span aria-hidden className={`${chipBase} border border-border text-[var(--line-2)]`}>
            →
          </span>
        )}
      </div>
    </div>
  );
}
