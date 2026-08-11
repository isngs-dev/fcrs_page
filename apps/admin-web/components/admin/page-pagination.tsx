/**
 * Generic Prev/Next page-chip pagination (SR-17), extracted from
 * `leads/leads-pagination.tsx`'s exact visual recipe so `/contacts` and
 * `/accounts` (D6: server-driven pagination, URL-encoded `?page=`) don't
 * duplicate it a second and third time. Purely presentational -- the caller
 * computes `hasPrevious`/`hasNext`/hrefs from its own list result.
 */
import Link from "next/link";

const chipBase =
  "grid min-h-9 min-w-9 place-items-center rounded-lg border border-border px-2.5 text-[12.5px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

export function PagePagination({
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
    <div className="flex items-center text-[12.5px] text-muted-foreground">
      <span>{rangeLabel}</span>
      <div className="ml-auto flex gap-1.5">
        {hasPrevious ? (
          <Link href={prevHref} scroll={false} aria-label="Previous page" className={`${chipBase} text-[var(--ink-2)] hover:bg-secondary`}>
            ←
          </Link>
        ) : (
          <span aria-hidden className={`${chipBase} text-[var(--line-2)]`}>
            ←
          </span>
        )}
        <span aria-current="page" className={`${chipBase} border-transparent bg-primary font-semibold text-primary-foreground`}>
          {page}
        </span>
        {hasNext ? (
          <Link href={nextHref} scroll={false} aria-label="Next page" className={`${chipBase} text-[var(--ink-2)] hover:bg-secondary`}>
            →
          </Link>
        ) : (
          <span aria-hidden className={`${chipBase} text-[var(--line-2)]`}>
            →
          </span>
        )}
      </div>
    </div>
  );
}
