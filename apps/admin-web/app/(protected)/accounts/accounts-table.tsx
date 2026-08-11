/**
 * Accounts table (SR-17 scope item 5, geometry rebuilt SR-27 slice 4, real
 * sort added SR-29). Columns verified against the real `AccountResponse`
 * (D5): Name (with domain folded underneath), Created. The mockup's
 * Industry, Contacts-count, Owner, and Status columns
 * (Console.dc.html:839-852) have NO backing field on `AccountResponse` --
 * none are rendered anywhere in this module. No client-side aggregation is
 * performed to fabricate a count (explicitly forbidden by D5). No checkbox
 * column, no filter funnels -- no bulk-mutation endpoint (G7) and no
 * filter-worthy field beyond what the toolbar `?q=` search already covers
 * (D-FILTER-ACCOUNTS, SR-29).
 *
 * SR-29 adds real sort (`name`, `domain`, `created` -- both name and domain
 * sort links live in the SAME "Account" header, since both fields render in
 * one visual cell) via the shared `ColumnSortLink` primitive, now that
 * `GET /admin/accounts` actually supports `?sort=&dir=`. This SUPERSEDES the
 * SR-17/SR-22/SR-27 "no sort" decision for this table specifically -- see
 * `dev_plan/handoffs/SR-29-crm-list-sort-filter-parity-handoff.md`.
 *
 * The 30x30 rounded-8 cream mono-initial tile + two-line name/domain stack
 * (Console.dc.html:845) replaces the old separate Name/Domain columns --
 * both `name` and `domain` are real fields, so this is a real value, not a
 * fabrication. Grid tracks apply only to the two surviving columns: `1.6fr`
 * (Account) and `110px` (Created) -- the 44px checkbox track and the four
 * unbacked tracks from the reference are not built.
 *
 * No edit affordance in this file at all -- there is no
 * `PATCH /admin/accounts/{id}` (M3), for any role.
 */
import type { Account, AccountSortDirection, AccountSortKey } from "@/lib/accounts";
import { defaultAccountSortDirection } from "@/lib/accounts";
import { TableCard, TableHeadCell, TableCell, TableRow } from "@/components/admin/table-card";
import { ColumnSortLink } from "@/components/admin/column-sort-link";

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function initialsFromName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
}

export function AccountsTable({
  items,
  basePath = "/accounts",
  currentParams,
  sort,
  direction,
}: {
  items: Account[];
  basePath?: string;
  currentParams?: URLSearchParams;
  sort?: AccountSortKey;
  direction?: AccountSortDirection;
}) {
  const resolvedParams = currentParams ?? new URLSearchParams();
  const nameActive = sort === "name";
  const domainActive = sort === "domain";
  const createdActive = sort === "created";

  return (
    <TableCard>
      <colgroup>
        <col style={{ width: "1.6fr" }} />
        <col style={{ width: "110px" }} />
      </colgroup>
      <thead>
        <tr>
          <TableHeadCell
            aria-sort={
              nameActive
                ? direction === "asc"
                  ? "ascending"
                  : "descending"
                : domainActive
                  ? direction === "asc"
                    ? "ascending"
                    : "descending"
                  : "none"
            }
            rightControls={
              <span className="flex items-center gap-1">
                <ColumnSortLink
                  label="Name"
                  sortKey="name"
                  basePath={basePath}
                  currentParams={resolvedParams}
                  currentSort={sort}
                  currentDirection={direction}
                  defaultDirection={defaultAccountSortDirection("name")}
                  dropParams={[]}
                />
                <ColumnSortLink
                  label="Domain"
                  sortKey="domain"
                  basePath={basePath}
                  currentParams={resolvedParams}
                  currentSort={sort}
                  currentDirection={direction}
                  defaultDirection={defaultAccountSortDirection("domain")}
                  dropParams={[]}
                />
              </span>
            }
          >
            Account
          </TableHeadCell>
          <TableHeadCell
            aria-sort={createdActive ? (direction === "asc" ? "ascending" : "descending") : "none"}
            rightControls={
              <ColumnSortLink
                label="Created"
                sortKey="created"
                basePath={basePath}
                currentParams={resolvedParams}
                currentSort={sort}
                currentDirection={direction}
                defaultDirection={defaultAccountSortDirection("created")}
                dropParams={[]}
              />
            }
          >
            Created
          </TableHeadCell>
        </tr>
      </thead>
      <tbody>
        {items.map((account) => (
          <TableRow key={account.accountId}>
            <TableCell>
              <span className="flex items-center gap-2.5">
                <span
                  aria-hidden
                  className="grid size-[30px] shrink-0 place-items-center rounded-lg bg-secondary text-[11px] font-bold text-[var(--ink-2)]"
                >
                  {initialsFromName(account.name)}
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-[13px] font-semibold text-foreground">
                    {account.name}
                  </span>
                  <span className="block truncate text-[12px] text-muted-foreground">
                    {account.domain || "—"}
                  </span>
                </span>
              </span>
            </TableCell>
            <TableCell className="text-muted-foreground">{formatDate(account.createdAt)}</TableCell>
          </TableRow>
        ))}
      </tbody>
    </TableCard>
  );
}
