/**
 * Contacts search toolbar (SR-29). A plain GET `<form>` over `?q=`, no
 * client JS -- mirrors `leads-filter.tsx`'s pattern. Also carries a hidden
 * `account_id` passthrough so submitting a new search doesn't silently drop
 * an active Company filter (set from the header funnel).
 */
import Link from "next/link";

export function ContactsFilter({
  currentQuery,
  currentAccountId,
  basePath = "/contacts",
}: {
  currentQuery: string | undefined;
  currentAccountId: string | undefined;
  basePath?: string;
}) {
  return (
    <form action={basePath} method="get" className="flex flex-wrap items-center gap-2.5">
      {currentAccountId ? <input type="hidden" name="account_id" value={currentAccountId} /> : null}
      <label htmlFor="contacts-q" className="sr-only">
        Search contacts
      </label>
      <input
        id="contacts-q"
        name="q"
        type="search"
        defaultValue={currentQuery ?? ""}
        placeholder="Search name or email…"
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
        <Link
          href={
            currentAccountId
              ? `${basePath}?account_id=${encodeURIComponent(currentAccountId)}`
              : basePath
          }
          className="text-[12.5px] text-muted-foreground underline underline-offset-2"
        >
          Clear
        </Link>
      ) : null}
    </form>
  );
}
