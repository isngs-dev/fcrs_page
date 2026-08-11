/**
 * Contacts page (SR-17, replaces SR-15 D7's placeholder). A `TableCard` list
 * over `GET /admin/contacts` (server-driven pagination, D6), a record
 * drawer over `GET /admin/contacts/{id}/timeline` (D3), and an "Add
 * contact" affordance rendered ONLY for `CLIENT_ADMIN` (D2) -- `CLIENT_AGENT`
 * gets the identical read surface with the button absent entirely, not
 * disabled.
 *
 * D1: this is a flat top-level route, NOT a tab inside `/leads` -- no
 * Leads/Contacts/Companies segmented control is rendered here or anywhere
 * else this sprint touches.
 *
 * All state (page, open drawer, timeline cursor, sort, search, company
 * filter) lives in the URL (`?page=`, `?contact=`, `?before=`, `?sort=`,
 * `?dir=`, `?q=`, `?account_id=`) -- no client state beyond the drawer's own
 * focus handling.
 *
 * SR-27 slice 5 (depends on slice 4's account pattern + reports' agent-name
 * pattern, `reports/page.tsx:113-120`): 28px/600 title; fetches the account
 * list ONCE (`listAccounts`, capped at the server's own 200-row page limit)
 * to build an `accountId -> name` map so the Company column can render a
 * real resolved name instead of the "View in Accounts" placeholder link --
 * falling back to that same honest link for any id the map can't resolve
 * (never a fabricated name). Reuses the exact `agentNames` pattern
 * `reports/page.tsx` already established from `listMembers()` for the Owner
 * column -- `GET /admin/users` is CLIENT_ADMIN-only (SR-25 F3), so for a
 * CLIENT_AGENT caller the map is simply empty and the column falls back to
 * the raw id/em-dash, never a broken or blank cell.
 *
 * SR-29: real `?sort=&dir=&q=&account_id=` now that `GET /admin/contacts`
 * supports them. The SAME `accountNames` map built for the Company column
 * also supplies the Company filter funnel's options (zero new fetches);
 * `accountsTruncated` is passed through so the funnel can honestly note when
 * it only lists the first page of accounts. `pageHref` now preserves every
 * active param -- the SR-25 "pagination silently drops the sort" bug class.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { listContacts, CONTACT_SORT_KEYS, type ContactSortKey } from "@/lib/contacts";
import { listAccounts } from "@/lib/accounts";
import { listMembers } from "@/lib/members";
import { ContactsTable } from "@/app/(protected)/contacts/contacts-table";
import { ContactsFilter } from "@/app/(protected)/contacts/contacts-filter";
import { PagePagination } from "@/components/admin/page-pagination";
import { AddContactForm } from "@/app/(protected)/contacts/add-contact-form";
import { ContactDrawerContainer } from "@/app/(protected)/contacts/contact-drawer-container";
import { SoftCard } from "@/components/admin/soft-card";

interface ContactsPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function pageHref(page: number, currentParams: URLSearchParams): string {
  const query = new URLSearchParams(currentParams);
  if (page > 1) {
    query.set("page", String(page));
  } else {
    query.delete("page");
  }
  const qs = query.toString();
  return qs ? `/contacts?${qs}` : "/contacts";
}

export default async function ContactsPage({ searchParams }: ContactsPageProps) {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawPage = Number.parseInt(firstValue(params.page) ?? "1", 10);
  const page = Number.isFinite(rawPage) && rawPage >= 1 ? rawPage : 1;
  const contactId = firstValue(params.contact);
  const before = firstValue(params.before);

  const rawSort = firstValue(params.sort);
  const sort =
    rawSort && (CONTACT_SORT_KEYS as readonly string[]).includes(rawSort)
      ? (rawSort as ContactSortKey)
      : undefined;
  const rawDirection = firstValue(params.dir);
  const direction = rawDirection === "asc" || rawDirection === "desc" ? rawDirection : undefined;
  const q = firstValue(params.q);
  const accountIdFilter = firstValue(params.account_id);

  // ?contact= and ?before= are drawer-local state that must survive a
  // sort/filter/search change; ?before= (the timeline cursor) is dropped
  // when the drawer itself isn't open, mirroring leads-filter.tsx.
  const currentParams = new URLSearchParams();
  if (page > 1) currentParams.set("page", String(page));
  if (sort) currentParams.set("sort", sort);
  if (direction) currentParams.set("dir", direction);
  if (q) currentParams.set("q", q);
  if (accountIdFilter) currentParams.set("account_id", accountIdFilter);
  if (contactId) currentParams.set("contact", contactId);

  const [result, accountsResult, membersResult] = await Promise.all([
    listContacts({ page, sort, direction, q, accountId: accountIdFilter }),
    // Best-effort accountId -> name map for the Company column (C8) AND the
    // Company filter funnel's option list (SR-29): one extra fetch of the
    // first accounts page, same bounded-page pattern as every other list on
    // this console. If an id isn't in the map (a contact whose account is
    // beyond this page), the table falls back to the honest "View in
    // Accounts" link -- never a fabricated name.
    listAccounts({ page: 1 }),
    // Owner-name map (C9), reusing reports/page.tsx:113-120's exact
    // pattern. CLIENT_AGENT gets an empty map (GET /admin/users is
    // CLIENT_ADMIN-only, SR-25 F3) -- the table falls back to the raw id.
    listMembers(),
  ]);

  const accountNames: Record<string, string> =
    accountsResult.status === "ok"
      ? Object.fromEntries(accountsResult.items.map((a) => [a.accountId, a.name]))
      : {};
  const accountsTruncated =
    accountsResult.status === "ok" && accountsResult.total > accountsResult.items.length;

  const agentNames: Record<string, string> =
    membersResult.status === "ok"
      ? Object.fromEntries(
          membersResult.items
            .filter((m): m is typeof m & { name: string } => m.name !== null)
            .map((m) => [m.id, m.name])
        )
      : {};

  // D2: "Add contact" backs POST /admin/contacts, CLIENT_ADMIN only
  // (contacts/admin_routes.py:197). Hidden entirely for CLIENT_AGENT, not
  // disabled -- see D2's rationale in the sprint spec.
  const canWrite = claims.role === "CLIENT_ADMIN";

  return (
    <div className="flex flex-1 flex-col gap-5 p-6 lg:p-8">
      <div className="flex flex-wrap items-center gap-3.5">
        <div>
          <h1 className="text-[28px] font-semibold text-foreground">Contacts</h1>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            The people behind your leads and accounts.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2.5">
          <ContactsFilter currentQuery={q} currentAccountId={accountIdFilter} />
          {canWrite ? <AddContactForm /> : null}
        </div>
      </div>

      {result.status === "error" ? (
        <p
          role="alert"
          className="rounded-[14px] border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]"
        >
          {result.message}
          {result.correlationId ? (
            <span className="block text-xs opacity-80">Correlation ID: {result.correlationId}</span>
          ) : null}
        </p>
      ) : result.items.length === 0 ? (
        <SoftCard className="flex flex-col items-center justify-center gap-2 p-12 text-center">
          <p className="text-sm font-semibold text-[var(--ink-2)]">
            {result.total === 0 ? "No contacts yet" : "No contacts on this page"}
          </p>
          <p className="max-w-sm text-xs text-muted-foreground">
            {result.total === 0
              ? "Contacts created manually or converted from qualified leads will appear here."
              : "Try an earlier page."}
          </p>
          {result.total > 0 && result.offset > 0 ? (
            <Link href={pageHref(page - 1, currentParams)} className="text-sm underline">
              Previous
            </Link>
          ) : null}
        </SoftCard>
      ) : (
        <>
          <ContactsTable
            items={result.items}
            basePath="/contacts"
            currentParams={currentParams}
            selectedContactId={contactId}
            accountNames={accountNames}
            ownerNames={agentNames}
            sort={sort}
            direction={direction}
            accountIdFilter={accountIdFilter}
            accountsTruncated={accountsTruncated}
          />
          <PagePagination
            page={page}
            hasPrevious={result.offset > 0}
            hasNext={result.offset + result.limit < result.total}
            prevHref={pageHref(page - 1, currentParams)}
            nextHref={pageHref(page + 1, currentParams)}
            rangeLabel={`Showing ${result.offset + 1}–${result.offset + result.items.length} of ${result.total}`}
          />
        </>
      )}

      {contactId ? (
        <ContactDrawerContainer contactId={contactId} before={before} basePath="/contacts" />
      ) : null}
    </div>
  );
}
