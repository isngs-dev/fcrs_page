/**
 * Accounts search toolbar (SR-29). A plain GET `<form>` over `?q=`, no
 * client JS -- mirrors `leads-filter.tsx`'s pattern. No "Filter" button: per
 * D-FILTER-ACCOUNTS, nothing on this table is filterable beyond what `?q=`
 * already covers (name/domain), so a generic Filter button would open onto
 * nothing.
 */
import Link from "next/link";

export function AccountsFilter({
  currentQuery,
  basePath = "/accounts",
}: {
  currentQuery: string | undefined;
  basePath?: string;
}) {
  return (
    <form action={basePath} method="get" className="flex flex-wrap items-center gap-2.5">
      <label htmlFor="accounts-q" className="sr-only">
        Search accounts
      </label>
      <input
        id="accounts-q"
        name="q"
        type="search"
        defaultValue={currentQuery ?? ""}
        placeholder="Search name or domain…"
        minLength={2}
        maxLength={200}
        className="h-[38px] w-56 rounded-[10px] border border-border bg-card px-3.5 text-[13.5px] text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-ring"
      />
      <button
        type="submit"
        className="inline-flex h-[38px] items-center rounded-[10px] border border-border bg-card px-4 text-[13.5px] font-semibold text-[var(--ink-2)] hover:bg-[#e6e6e6]"
      >
        Search
      </button>
      {currentQuery ? (
        <Link href={basePath} className="text-[12.5px] text-muted-foreground underline underline-offset-2">
          Clear
        </Link>
      ) : null}
    </form>
  );
}
