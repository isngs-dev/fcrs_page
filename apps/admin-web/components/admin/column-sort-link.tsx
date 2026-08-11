/** Server-rendered, bookmarkable three-state sort control (SR-25 D10, moved
 * to components/admin/ and generalized off Leads-specific types by SR-29 so
 * Contacts/Accounts can share it too). */
import Link from "next/link";

function hrefForSort({
  basePath,
  currentParams,
  sortKey,
  currentSort,
  currentDirection,
  defaultDirection,
  dropParams,
}: {
  basePath: string;
  currentParams: URLSearchParams;
  sortKey: string;
  currentSort?: string;
  currentDirection?: "asc" | "desc";
  defaultDirection: "asc" | "desc";
  dropParams: string[];
}): string {
  const params = new URLSearchParams(currentParams);
  params.delete("page");
  for (const key of dropParams) {
    params.delete(key);
  }

  if (currentSort !== sortKey) {
    params.set("sort", sortKey);
    params.set("dir", defaultDirection);
  } else if (currentDirection === defaultDirection) {
    params.set("dir", defaultDirection === "asc" ? "desc" : "asc");
  } else {
    params.delete("sort");
    params.delete("dir");
  }

  const query = params.toString();
  return query ? `${basePath}?${query}` : basePath;
}

export function ColumnSortLink({
  label,
  sortKey,
  basePath,
  currentParams,
  currentSort,
  currentDirection,
  defaultDirection,
  dropParams = ["lead", "tab"],
}: {
  label: string;
  sortKey: string;
  basePath: string;
  currentParams: URLSearchParams;
  currentSort?: string;
  currentDirection?: "asc" | "desc";
  /** The direction this column sorts in on its first click -- callers
   * resolve this from their own table's sort-key -> default-direction map
   * (e.g. `defaultLeadSortDirection`, `ACCOUNT_SORT_DEFAULT_DIRECTIONS`). */
  defaultDirection: "asc" | "desc";
  /** Extra query params to drop alongside `page` when building a sort link
   * -- deleting an absent param is a no-op, so each consumer just names
   * whatever detail-view/cursor params it owns. Defaults to Leads' original
   * behavior. */
  dropParams?: string[];
}) {
  const active = currentSort === sortKey;
  const glyph = active && currentDirection === "asc" ? "↑" : active ? "↓" : "↕";
  const nextAction = !active
    ? `Sort ${label} ${defaultDirection}ending`
    : currentDirection === defaultDirection
      ? `Sort ${label} ${defaultDirection === "asc" ? "descending" : "ascending"}`
      : `Clear ${label} sort`;

  return (
    <Link
      href={hrefForSort({
        basePath,
        currentParams,
        sortKey,
        currentSort,
        currentDirection,
        defaultDirection,
        dropParams,
      })}
      scroll={false}
      aria-label={nextAction}
      className="grid size-5 place-items-center rounded text-[13px] leading-none text-muted-foreground hover:bg-[#e6e6e6] hover:text-[var(--ink-2)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring"
    >
      <span aria-hidden>{glyph}</span>
    </Link>
  );
}
