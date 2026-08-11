/** Server-rendered <details> funnel menu for a real list filter (SR-25, moved
 * to components/admin/ and shared across Leads/Contacts/Accounts by SR-29). */
import Link from "next/link";

export interface ColumnFilterOption {
  label: string;
  values: Record<string, string | undefined>;
  active?: boolean;
}

function hrefForFilter(
  basePath: string,
  currentParams: URLSearchParams,
  values: Record<string, string | undefined>,
  dropParams: string[]
): string {
  const params = new URLSearchParams(currentParams);
  params.delete("page");
  for (const key of dropParams) {
    params.delete(key);
  }
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined) {
      params.delete(key);
    } else {
      params.set(key, value);
    }
  }
  const query = params.toString();
  return query ? `${basePath}?${query}` : basePath;
}

export function ColumnFilterMenu({
  label,
  basePath,
  currentParams,
  options,
  unavailableMessage,
  dropParams = ["lead", "tab"],
}: {
  label: string;
  basePath: string;
  currentParams: URLSearchParams;
  options: ColumnFilterOption[];
  unavailableMessage?: string;
  /** Extra query params to drop alongside `page` when building a filter
   * link -- deleting an absent param is a no-op, so each consumer just
   * names whatever detail-view/cursor params it owns (Leads: `lead`/`tab`;
   * Contacts: `contact`/`before`). Defaults to Leads' original behavior. */
  dropParams?: string[];
}) {
  return (
    <details className="group relative">
      <summary
        aria-label={`Filter ${label}`}
        className="grid size-5 cursor-pointer list-none place-items-center rounded text-muted-foreground hover:bg-[#e6e6e6] hover:text-[var(--ink-2)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring [&::-webkit-details-marker]:hidden"
      >
        <svg aria-hidden width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 6h16M7 12h10M10 18h4" />
        </svg>
      </summary>
      <div className="absolute top-6 right-0 z-20 min-w-40 rounded-[10px] border border-border bg-card p-1.5 text-left shadow-md">
        {options.map((option) => (
          <Link
            key={`${option.label}-${JSON.stringify(option.values)}`}
            href={hrefForFilter(basePath, currentParams, option.values, dropParams)}
            scroll={false}
            className={`block rounded-md px-2.5 py-1.5 text-[12px] normal-case tracking-normal ${
              option.active
                ? "bg-secondary font-semibold text-foreground"
                : "text-[var(--ink-2)] hover:bg-secondary"
            }`}
          >
            {option.label}
          </Link>
        ))}
        {unavailableMessage ? (
          <p className="px-2.5 py-1.5 text-[11px] normal-case tracking-normal text-muted-foreground">
            {unavailableMessage}
          </p>
        ) : null}
      </div>
    </details>
  );
}
