/**
 * Contacts table (SR-17 scope item 4, geometry + name-resolution rebuilt
 * SR-27 slice 5, real sort/filter/search added SR-29). Columns verified
 * against the real `ContactResponse` (D5): Name, Email, Company, Owner,
 * Created. No checkbox column (G7 -- no bulk endpoint). No Status chip
 * column (G9 -- `ContactResponse` has no `status` field). Created is KEPT
 * despite the reference's Name/Email/Company/Owner/Status five-column set,
 * because Status can't be built -- dropping Created too would leave this
 * page thinner than both the reference and the prior shipped version (D12).
 *
 * SR-29 supersedes the prior "no sort icons, no filter funnels" note: now
 * that `GET /admin/contacts` supports `?sort=&dir=&q=&account_id=`, every
 * column gets a real sort link (Name/Email/Owner/Created alphabetically or
 * by recency; Company via `?sort=account`, labelled "Group by company" --
 * D-COMPANY-SORT: `account_id` is an opaque id, so this GROUPS same-company
 * rows adjacently, it does NOT alpha-sort by company name). Company is also
 * the only column with a real filter funnel (`?account_id=`) -- Owner
 * deliberately has none: `GET /admin/users` is CLIENT_ADMIN-only (SR-25 F3),
 * so an owner-name dropdown would be a dead control for CLIENT_AGENT.
 *
 * Company column (G8): `ContactResponse` carries `accountId` only, no join
 * to a name. `page.tsx` fetches the account list once server-side and
 * builds an `accountId -> name` map (mirroring `reports/page.tsx`'s
 * `agentNames` pattern from `listMembers()`), passed in as `accountNames`.
 * When the map resolves the id, the real account name renders as the link
 * text; when it can't (id outside the fetched page, or the map fetch
 * failed), this falls back to the honest "View in Accounts" link -- never a
 * fabricated name. The SAME map also supplies the Company filter funnel's
 * option labels (zero new fetches) -- when `accountsResult.total` exceeds
 * the fetched page, `page.tsx` passes an `accountsTruncated` note so the
 * funnel can say so honestly rather than silently listing only some.
 *
 * Owner column (G8/SR-25 F3): same map pattern via `ownerNames`, built from
 * `listMembers()`. `GET /admin/users` is CLIENT_ADMIN-only, so for a
 * CLIENT_AGENT the map is empty and every row falls back to an em-dash
 * (never the raw internal id, never a broken cell).
 *
 * Each row is a `<Link>` (not a client onClick) opening `?contact=<id>`,
 * matching `leads-table.tsx`'s progressive-enhancement convention exactly.
 */
import Link from "next/link";
import type { Contact, ContactSortDirection, ContactSortKey } from "@/lib/contacts";
import { defaultContactSortDirection } from "@/lib/contacts";
import { TableCard, TableHeadCell, TableCell, TableRow } from "@/components/admin/table-card";
import { ColumnSortLink } from "@/components/admin/column-sort-link";
import { ColumnFilterMenu, type ColumnFilterOption } from "@/components/admin/column-filter-menu";

const MUTED = <span className="text-muted-foreground">—</span>;
const DROP_PARAMS = ["contact", "before"];

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function initials(name: string | null): string {
  const trimmed = (name ?? "").trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/);
  if (parts.length === 1) return trimmed.slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function contactHref(basePath: string, currentParams: URLSearchParams, contactId: string): string {
  const params = new URLSearchParams(currentParams);
  params.set("contact", contactId);
  params.delete("before");
  return `${basePath}?${params.toString()}`;
}

interface HeaderDescriptor {
  key: string;
  label: string;
  sortKey?: ContactSortKey;
  filter?: "account";
}

const HEADERS: HeaderDescriptor[] = [
  { key: "name", label: "Name", sortKey: "name" },
  { key: "email", label: "Email", sortKey: "email" },
  { key: "company", label: "Company", sortKey: "account", filter: "account" },
  { key: "owner", label: "Owner", sortKey: "owner" },
  { key: "created", label: "Created", sortKey: "created" },
];

export function ContactsTable({
  items,
  basePath,
  currentParams,
  selectedContactId,
  accountNames = {},
  ownerNames = {},
  sort,
  direction,
  accountIdFilter,
  accountsTruncated = false,
}: {
  items: Contact[];
  basePath?: string;
  currentParams?: URLSearchParams;
  selectedContactId?: string;
  /** `accountId -> name` map, built once server-side (G8). A missing entry
   * falls back to the honest "View in Accounts" link -- never fabricated. */
  accountNames?: Record<string, string>;
  /** `userId -> name` map from `listMembers()` (reused from
   * `reports/page.tsx`'s `agentNames` pattern). Empty for CLIENT_AGENT
   * (SR-25 F3: `GET /admin/users` is CLIENT_ADMIN-only). */
  ownerNames?: Record<string, string>;
  sort?: ContactSortKey;
  direction?: ContactSortDirection;
  /** The active `?account_id=` filter value, if any (SR-29 D-FILTER). */
  accountIdFilter?: string;
  /** True when `accountNames` covers fewer accounts than the tenant has
   * (the Company filter funnel's option list is capped at one fetched
   * page) -- rendered as an honest truncation note, never silently. */
  accountsTruncated?: boolean;
}) {
  const resolvedBasePath = basePath ?? "/contacts";
  const resolvedParams = currentParams ?? new URLSearchParams();

  const companyFilterOptions: ColumnFilterOption[] = [
    { label: "All companies", values: { account_id: undefined }, active: !accountIdFilter },
    ...Object.entries(accountNames).map(([accountId, name]) => ({
      label: name,
      values: { account_id: accountId },
      active: accountIdFilter === accountId,
    })),
  ];

  return (
    <TableCard>
      <colgroup>
        <col style={{ width: "1.3fr" }} />
        <col style={{ width: "1.6fr" }} />
        <col style={{ width: "1.1fr" }} />
        <col style={{ width: "1fr" }} />
        <col style={{ width: "110px" }} />
      </colgroup>
      <thead>
        <tr>
          {HEADERS.map((header) => {
            const active = header.sortKey && sort === header.sortKey;
            return (
              <TableHeadCell
                key={header.key}
                aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}
                rightControls={
                  header.sortKey ? (
                    <span className="flex items-center gap-1">
                      <ColumnSortLink
                        label={header.sortKey === "account" ? "Group by company" : header.label}
                        sortKey={header.sortKey}
                        basePath={resolvedBasePath}
                        currentParams={resolvedParams}
                        currentSort={sort}
                        currentDirection={direction}
                        defaultDirection={defaultContactSortDirection(header.sortKey)}
                        dropParams={DROP_PARAMS}
                      />
                      {header.filter === "account" ? (
                        <ColumnFilterMenu
                          label="Company"
                          basePath={resolvedBasePath}
                          currentParams={resolvedParams}
                          options={companyFilterOptions}
                          dropParams={DROP_PARAMS}
                          unavailableMessage={
                            accountsTruncated
                              ? `Showing the first ${Object.keys(accountNames).length} accounts.`
                              : undefined
                          }
                        />
                      ) : null}
                    </span>
                  ) : undefined
                }
              >
                {header.label}
              </TableHeadCell>
            );
          })}
        </tr>
      </thead>
      <tbody>
        {items.map((contact) => {
          const highlighted = contact.contactId === selectedContactId;
          const resolvedAccountName = contact.accountId ? accountNames[contact.accountId] : undefined;
          const resolvedOwnerName = contact.ownerAgentId ? ownerNames[contact.ownerAgentId] : undefined;
          return (
            <TableRow key={contact.contactId} className={highlighted ? "bg-secondary" : undefined}>
              <TableCell className="px-0 py-0">
                <Link
                  href={contactHref(resolvedBasePath, resolvedParams, contact.contactId)}
                  scroll={false}
                  className="flex min-h-11 items-center gap-2.5 px-3.5 py-3 font-semibold text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                >
                  <span className="grid size-7 shrink-0 place-items-center rounded-full bg-border text-[10px] font-bold text-[var(--ink-2)]">
                    {initials(contact.name)}
                  </span>
                  {contact.name || "Unnamed contact"}
                </Link>
              </TableCell>
              <TableCell className="text-muted-foreground">{contact.email || MUTED}</TableCell>
              <TableCell>
                {contact.accountId ? (
                  resolvedAccountName ? (
                    <Link href="/accounts" className="text-foreground underline underline-offset-2">
                      {resolvedAccountName}
                    </Link>
                  ) : (
                    <Link
                      href="/accounts"
                      className="text-foreground underline underline-offset-2"
                      title={`Account ${contact.accountId} — there is no per-account detail page yet (D5); this links to the Accounts list.`}
                    >
                      View in Accounts →
                    </Link>
                  )
                ) : (
                  MUTED
                )}
              </TableCell>
              <TableCell className="text-muted-foreground">
                {resolvedOwnerName ?? contact.ownerAgentId ?? "—"}
              </TableCell>
              <TableCell className="text-muted-foreground">{formatDate(contact.createdAt)}</TableCell>
            </TableRow>
          );
        })}
      </tbody>
    </TableCard>
  );
}
