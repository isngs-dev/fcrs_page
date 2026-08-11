/**
 * Per-client lead review screen (S13.7), restyled alongside `leads/page.tsx`
 * to the 4b screen. Reuses the same restyled `LeadsFilter`/`LeadsTable`/
 * `LeadsPagination`/`LeadsViewToggle`/`LeadDrawerContainer`, parameterized by
 * the route's `{tenantId}` (D1) so every fetch targets the S12.7
 * PLATFORM_ADMIN super-user surface `/admin/tenants/{tenantId}/leads*`
 * instead of the implicit `/admin/leads*`. Mirrors `leads/page.tsx`'s
 * server-first architecture exactly (URL searchParams drive filter/
 * pagination/view/drawer state, no client state beyond the drawer itself).
 */
import Link from "next/link";
import {
  defaultLeadSortDirection,
  LEAD_SORT_KEYS,
  LEAD_STAGES,
  LEAD_STATUSES,
  listLeads,
  type LeadSortDirection,
} from "@/lib/leads";
import { requireRole } from "@/lib/auth";
import { listMembersForTenant } from "@/lib/members";
import { LeadsFilter } from "@/app/(protected)/leads/leads-filter";
import { LeadsTable } from "@/app/(protected)/leads/leads-table";
import { LeadsPagination } from "@/app/(protected)/leads/leads-pagination";
import { LeadsViewToggle } from "@/app/(protected)/leads/leads-view-toggle";
import { LeadDrawerContainer } from "@/app/(protected)/leads/lead-drawer-container";

interface ClientLeadsPageProps {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export default async function ClientLeadsPage({ params, searchParams }: ClientLeadsPageProps) {
  const claims = await requireRole("PLATFORM_ADMIN");
  const { tenantId } = await params;
  const basePath = `/clients/${tenantId}/leads`;

  function pageHref(page: number, currentParams: URLSearchParams): string {
    const query = new URLSearchParams(currentParams);
    query.delete("lead");
    query.delete("tab");
    if (page > 1) query.set("page", String(page));
    else query.delete("page");
    const qs = query.toString();
    return qs ? `${basePath}?${qs}` : basePath;
  }

  const resolvedSearchParams = await searchParams;
  const rawStage = firstValue(resolvedSearchParams.stage);
  const stage =
    rawStage && (LEAD_STAGES as readonly string[]).includes(rawStage) ? rawStage : undefined;
  const rawStatus = firstValue(resolvedSearchParams.status);
  const status =
    rawStatus && (LEAD_STATUSES as readonly string[]).includes(rawStatus) ? rawStatus : undefined;
  const rawAssignedAgentId = firstValue(resolvedSearchParams.assigned_agent_id)?.trim();
  const assignedAgentId = rawAssignedAgentId && rawAssignedAgentId.length <= 200
    ? rawAssignedAgentId
    : undefined;
  const parseDateParam = (value: string | undefined) =>
    value && !Number.isNaN(Date.parse(value)) ? value : undefined;
  const createdFrom = parseDateParam(firstValue(resolvedSearchParams.from));
  const createdTo = parseDateParam(firstValue(resolvedSearchParams.to));
  const rawQ = firstValue(resolvedSearchParams.q)?.trim();
  const q = rawQ && rawQ.length >= 2 && rawQ.length <= 200 ? rawQ : undefined;
  const rawSort = firstValue(resolvedSearchParams.sort);
  const sort = rawSort && (LEAD_SORT_KEYS as readonly string[]).includes(rawSort)
    ? rawSort
    : undefined;
  const rawDirection = firstValue(resolvedSearchParams.dir);
  const direction: LeadSortDirection | undefined = sort
    ? rawDirection === "asc" || rawDirection === "desc"
      ? rawDirection
      : defaultLeadSortDirection(sort as (typeof LEAD_SORT_KEYS)[number])
    : undefined;
  const rawPage = Number.parseInt(firstValue(resolvedSearchParams.page) ?? "1", 10);
  const page = Number.isFinite(rawPage) && rawPage >= 1 ? rawPage : 1;
  const view = firstValue(resolvedSearchParams.view) === "board" ? "board" : "table";
  const leadId = firstValue(resolvedSearchParams.lead);
  const tab = firstValue(resolvedSearchParams.tab);

  const currentParams = new URLSearchParams();
  if (view === "table") {
    if (page > 1) currentParams.set("page", String(page));
    if (stage) currentParams.set("stage", stage);
    if (status) currentParams.set("status", status);
    if (assignedAgentId) currentParams.set("assigned_agent_id", assignedAgentId);
    if (createdFrom) currentParams.set("from", createdFrom);
    if (createdTo) currentParams.set("to", createdTo);
    if (q) currentParams.set("q", q);
    if (sort && direction) {
      currentParams.set("sort", sort);
      currentParams.set("dir", direction);
    }
  }
  if (view === "board") currentParams.set("view", view);

  const result = await listLeads(
    view === "table"
      ? { page, stage, status, assignedAgentId, createdFrom, createdTo, q, sort, direction }
      : { page: 1 },
    tenantId
  );
  // The tenant-explicit API derives CLIENT_ADMIN claims for this PLATFORM_ADMIN
  // screen, so its active target-tenant agent list uses the same semantics as
  // the implicit client-admin table. Fetch it only after leads succeed.
  const membersResult =
    view === "table" && result.status === "ok" && result.items.length > 0
      ? await listMembersForTenant(tenantId)
      : undefined;
  const agents = membersResult?.status === "ok"
    ? membersResult.items
        .filter((member) => member.role === "CLIENT_AGENT" && member.active)
        .map((member) => ({ id: member.id, label: member.name ?? member.email }))
    : undefined;

  return (
    <div className="flex flex-1 flex-col gap-4 p-6 lg:p-8">
      <div className="flex flex-wrap items-center gap-3.5">
        <h1 className="text-[28px] font-semibold tracking-[-0.01em] text-[var(--foreground)]">Leads</h1>
        <LeadsViewToggle view={view} basePath={basePath} currentParams={currentParams} />
        <div className="ml-auto flex items-center gap-2.5">
          <LeadsFilter
            currentStage={stage}
            currentQ={q}
            currentParams={currentParams}
            basePath={basePath}
          />
        </div>
      </div>

      {view === "board" ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-[14px] border border-dashed border-[#d5d5cb] p-12 text-center">
          <p className="text-sm font-semibold text-[var(--ink-2)]">Board view is coming soon</p>
          <p className="max-w-sm text-xs text-[var(--muted-foreground)]">
            Kanban drag-and-drop between stages is a follow-up; see the Leads screen for the same
            scope note.
          </p>
          <Link href={basePath} className="mt-1 text-xs font-semibold text-[var(--foreground)] underline underline-offset-2">
            Switch to Table
          </Link>
        </div>
      ) : result.status === "error" ? (
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
        <div className="flex flex-col gap-3">
          <p role="status" className="rounded-[14px] border border-[var(--border)] bg-[var(--secondary)] p-4 text-sm text-[var(--ink-2)]">
          {result.total === 0 && (stage || status || assignedAgentId || createdFrom || createdTo || q) ? (
              <>
                No leads match this filter.{" "}
                <Link href={basePath} className="underline">
                  Clear filter
                </Link>
              </>
            ) : result.total === 0 ? (
              "No leads yet -- leads captured by this client's chatbot will appear here."
            ) : (
              "No leads on this page."
            )}
          </p>
          {result.total > 0 && result.offset > 0 ? (
            <Link href={pageHref(page - 1, currentParams)} className="text-sm underline">
              Previous
            </Link>
          ) : null}
        </div>
      ) : (
        <LeadsTable
          items={result.items}
          basePath={basePath}
          currentParams={currentParams}
          selectedLeadId={leadId}
          sort={sort}
          direction={direction}
          currentRole="CLIENT_ADMIN"
          currentUserId={claims.subject}
          agents={agents}
          agentsUnavailable={membersResult?.status === "error"}
          footer={
            <LeadsPagination
              page={page}
              hasPrevious={result.offset > 0}
              hasNext={result.offset + result.limit < result.total}
              prevHref={pageHref(page - 1, currentParams)}
              nextHref={pageHref(page + 1, currentParams)}
              rangeLabel={`Showing ${result.offset + 1}–${result.offset + result.items.length} of ${result.total}`}
            />
          }
        />
      )}

      {leadId ? (
        <LeadDrawerContainer leadId={leadId} rawTab={tab} basePath={basePath} tenantId={tenantId} />
      ) : null}
    </div>
  );
}
