/**
 * Accounts page (SR-17, replaces SR-15 D7's placeholder). A `TableCard`
 * list over `GET /admin/accounts` (server-driven pagination, D6, respecting
 * the endpoint's own 1-200 `limit` clamp rather than re-implementing it),
 * with an "Add account" affordance rendered ONLY for `CLIENT_ADMIN` (D2).
 *
 * No row click / drawer here -- accounts have no timeline endpoint and no
 * PATCH (M3); there is nothing for a detail view to show or edit yet. No
 * "Edit account" UI is built anywhere (D2/M3: the endpoint does not exist).
 *
 * D1: flat top-level route, label always "Accounts", never "Companies".
 *
 * SR-27 slice 4: 28px/600 title (was `text-xl`); no segmented
 * Leads/Contacts/Companies pill (D0, LOCKED, third confirmation -- see
 * `dev_plan/handoffs/SR-27-remaining-pages-fidelity-handoff.md` §0).
 *
 * SR-29: real `?sort=&dir=&q=` now that `GET /admin/accounts` supports them
 * (superseding SR-27's "no sort, no filter" note above for this table).
 * Still no generic "Filter" toolbar button -- per D-FILTER-ACCOUNTS nothing
 * on this table is filterable beyond what the `?q=` search box already
 * covers, so a Filter button would open onto nothing. `pageHref` now
 * preserves `sort`/`dir`/`q` -- the SR-25/SR-29 "pagination silently drops
 * the active sort" bug class this project has hit before.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { listAccounts, ACCOUNT_SORT_KEYS, type AccountSortKey } from "@/lib/accounts";
import { AccountsTable } from "@/app/(protected)/accounts/accounts-table";
import { AccountsFilter } from "@/app/(protected)/accounts/accounts-filter";
import { PagePagination } from "@/components/admin/page-pagination";
import { AddAccountForm } from "@/app/(protected)/accounts/add-account-form";
import { SoftCard } from "@/components/admin/soft-card";

interface AccountsPageProps {
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
  return qs ? `/accounts?${qs}` : "/accounts";
}

export default async function AccountsPage({ searchParams }: AccountsPageProps) {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawPage = Number.parseInt(firstValue(params.page) ?? "1", 10);
  const page = Number.isFinite(rawPage) && rawPage >= 1 ? rawPage : 1;

  const rawSort = firstValue(params.sort);
  const sort =
    rawSort && (ACCOUNT_SORT_KEYS as readonly string[]).includes(rawSort)
      ? (rawSort as AccountSortKey)
      : undefined;
  const rawDirection = firstValue(params.dir);
  const direction = rawDirection === "asc" || rawDirection === "desc" ? rawDirection : undefined;
  const q = firstValue(params.q);

  const currentParams = new URLSearchParams();
  if (page > 1) currentParams.set("page", String(page));
  if (sort) currentParams.set("sort", sort);
  if (direction) currentParams.set("dir", direction);
  if (q) currentParams.set("q", q);

  const result = await listAccounts({ page, sort, direction, q });

  // D2: "Add account" backs POST /admin/accounts, CLIENT_ADMIN only
  // (accounts/admin_routes.py:108). Hidden entirely for CLIENT_AGENT.
  const canWrite = claims.role === "CLIENT_ADMIN";

  return (
    <div className="flex flex-1 flex-col gap-5 p-6 lg:p-8">
      <div className="flex flex-wrap items-center gap-3.5">
        <div>
          <h1 className="text-[28px] font-semibold text-foreground">Accounts</h1>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            The organizations your contacts and deals belong to.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2.5">
          <AccountsFilter currentQuery={q} />
          {canWrite ? <AddAccountForm /> : null}
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
            {result.total === 0 ? "No accounts yet" : "No accounts on this page"}
          </p>
          <p className="max-w-sm text-xs text-muted-foreground">
            {result.total === 0
              ? "Accounts created manually or linked from contacts will appear here."
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
          <AccountsTable
            items={result.items}
            basePath="/accounts"
            currentParams={currentParams}
            sort={sort}
            direction={direction}
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
    </div>
  );
}
