/**
 * Lead review console (S13.4), restyled to HANDOFF-SPEC.md's "4b" screen:
 * segmented Board/Table toggle, restyled table + badges, bordered pagination
 * chips, and a 440px right-side lead detail drawer. Still gated by
 * `requireAnyRole` (decision 2), still an `async` server component that
 * reads all state (filter/pagination/view/drawer) from the URL
 * `searchParams` -- the drawer's own tab/focus handling (`lead-drawer.tsx`)
 * and the board's drag interaction (`leads-board.tsx`, M9) are the only
 * client state on this route.
 *
 * `?lead=<id>` opens the drawer (fetched by `LeadDrawerContainer`); `?tab=`
 * selects its active tab; `?view=board` renders the REAL kanban board
 * (SR-18 M6/scope item 8) -- `leads-view-toggle.tsx`'s honest "coming soon"
 * panel from SR-15 is retired by this sprint, since the board it was
 * standing in for now exists; `?stage=`/`?page=` (Table view only --
 * `?view=board` ignores both, see `LeadsBoardSection`) are unchanged from
 * S13.4.
 */
import Link from "next/link";
import { requireAnyRole, type Role } from "@/lib/auth";
import {
  defaultLeadSortDirection,
  LEAD_SORT_KEYS,
  LEAD_STAGES,
  LEAD_STATUSES,
  listAllLeadsForBoard,
  listLeads,
  type LeadSortDirection,
} from "@/lib/leads";
import { listMembers } from "@/lib/members";
import { LeadsFilter } from "@/app/(protected)/leads/leads-filter";
import { LeadsTable } from "@/app/(protected)/leads/leads-table";
import { LeadsPagination } from "@/app/(protected)/leads/leads-pagination";
import { LeadsViewToggle } from "@/app/(protected)/leads/leads-view-toggle";
import { LeadDrawerContainer } from "@/app/(protected)/leads/lead-drawer-container";
import { LeadsBoard } from "@/app/(protected)/leads/leads-board";

interface LeadsPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function pageHref(page: number, currentParams: URLSearchParams): string {
  const query = new URLSearchParams(currentParams);
  query.delete("lead");
  query.delete("tab");
  if (page > 1) query.set("page", String(page));
  else query.delete("page");
  const qs = query.toString();
  return qs ? `/leads?${qs}` : "/leads";
}

export default async function LeadsPage({ searchParams }: LeadsPageProps) {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const rawStage = firstValue(params.stage);
  const stage =
    rawStage && (LEAD_STAGES as readonly string[]).includes(rawStage) ? rawStage : undefined;
  const rawStatus = firstValue(params.status);
  const status =
    rawStatus && (LEAD_STATUSES as readonly string[]).includes(rawStatus) ? rawStatus : undefined;
  const rawAssignedAgentId = firstValue(params.assigned_agent_id)?.trim();
  const assignedAgentId = rawAssignedAgentId && rawAssignedAgentId.length <= 200
    ? rawAssignedAgentId
    : undefined;
  const parseDateParam = (value: string | undefined) =>
    value && !Number.isNaN(Date.parse(value)) ? value : undefined;
  const createdFrom = parseDateParam(firstValue(params.from));
  const createdTo = parseDateParam(firstValue(params.to));
  const rawQ = firstValue(params.q)?.trim();
  const q = rawQ && rawQ.length >= 2 && rawQ.length <= 200 ? rawQ : undefined;
  const rawSort = firstValue(params.sort);
  const sort = rawSort && (LEAD_SORT_KEYS as readonly string[]).includes(rawSort)
    ? rawSort
    : undefined;
  const rawDirection = firstValue(params.dir);
  const direction: LeadSortDirection | undefined = sort
    ? rawDirection === "asc" || rawDirection === "desc"
      ? rawDirection
      : defaultLeadSortDirection(sort as (typeof LEAD_SORT_KEYS)[number])
    : undefined;
  const rawPage = Number.parseInt(firstValue(params.page) ?? "1", 10);
  const page = Number.isFinite(rawPage) && rawPage >= 1 ? rawPage : 1;
  const view = firstValue(params.view) === "board" ? "board" : "table";
  const leadId = firstValue(params.lead);
  const tab = firstValue(params.tab);
  const before = firstValue(params.before);

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

  const hasActiveTableModifier = Boolean(
    stage || status || assignedAgentId || createdFrom || createdTo || q || sort
  );

  // The Board view is not paginated/stage-filtered like the Table view --
  // SR-18 D2's constrained-drop-target board needs to see every stage at
  // once (a card's legal targets are other columns on the SAME board), so
  // when `view==="board"` it fetches unfiltered at the backend's own
  // `[1,200]` limit clamp (admin_routes.py) instead of reusing `listLeads`,
  // which is hardcoded to `LEADS_PAGE_SIZE=25` for the Table view. The
  // Table-view `result` fetch is skipped entirely on the board branch to
  // avoid a wasted second round trip (M9: board is the first surface that
  // deliberately diverges from the page's single per-navigation fetch, and
  // this keeps that divergence to exactly the one extra call it needs).
  return (
    <div className="flex flex-1 flex-col gap-4 p-6 lg:p-8">
      {/* SR-15 D6 (LOCKED): NO Leads/Contacts/Companies segmented tab strip
          here -- Contacts/Accounts/Deals are flat top-level nav entries
          instead (see admin-shell.tsx). Only the existing Table|Board toggle
          remains. SR-24: a later design reference / user screenshot
          (Console.dc.html) again shows this pill; it remains DELIBERATELY
          unbuilt per the locked SR-15 D6 decision plus the D-naming rule
          ("Accounts" everywhere, never "Companies", no relabeling layer). Do
          not re-raise this a third time -- reopening D6 requires new
          evidence presented to the user, not a fresh design reference alone
          (CLAUDE.md §6b "decide once, write it down, cite it"). */}
      <div className="flex flex-wrap items-center gap-3.5">
        <h1 className="text-[28px] font-semibold tracking-[-0.01em] text-foreground">Leads</h1>
        <LeadsViewToggle view={view} basePath="/leads" currentParams={currentParams} />
        <div className="ml-auto flex items-center gap-2.5">
          <LeadsFilter currentStage={stage} currentQ={q} currentParams={currentParams} />
          {/* SR-22 M6: wired live -- `/leads/export` proxies
              `GET /admin/leads/export` (already exists server-side,
              tenant-scoped by `claims.tenant_id`), forwarding the session
              cookie the same way `reports/csv/[report]/route.ts` does.
              SR-24: restyled to the reference's `.btn-dark` recipe -- the
              href/wiring itself is untouched. */}
          <a
            href="/leads/export"
            className="inline-flex h-[38px] items-center gap-2 rounded-[10px] bg-[#333333] px-4 text-[13.5px] font-semibold whitespace-nowrap text-[#fbfaf7] transition-colors hover:bg-[#404040] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 3v12M7 10l5 5 5-5M5 21h14" />
            </svg>
            Export CSV
          </a>
          {view === "table" && hasActiveTableModifier ? (
            <span className="text-[11px] text-muted-foreground" title="Exports all leads, ignoring filters.">
              Exports all leads, ignoring filters
            </span>
          ) : null}
        </div>
      </div>

      {view === "board"
        ? await renderLeadsBoardSection(claims.role)
        : await renderLeadsTableSection({
          page,
          stage,
          status,
          assignedAgentId,
          createdFrom,
          createdTo,
          q,
          sort,
          direction,
          leadId,
          currentParams,
          currentRole: claims.role,
          currentUserId: claims.subject,
        })}

      {leadId ? (
        <LeadDrawerContainer leadId={leadId} rawTab={tab} basePath="/leads" before={before} />
      ) : null}
    </div>
  );
}

/**
 * The Board view is not paginated/stage-filtered like the Table view --
 * SR-18 D2's constrained-drop-target board needs to see every stage at once
 * (a card's legal targets are other columns on the SAME board), so it
 * fetches unfiltered at the backend's own `[1,200]` limit clamp
 * (leads/admin_routes.py) instead of reusing `listLeads`'s
 * `LEADS_PAGE_SIZE=25`.
 *
 * Rendered directly (`await renderLeadsBoardSection()` inside `LeadsPage`'s
 * own JSX), not as `<LeadsBoardSection />`, so this branch resolves BEFORE
 * `LeadsPage` returns -- an async component embedded as a JSX child works
 * in the real Next.js RSC runtime, but `react-dom/server`'s
 * `renderToStaticMarkup` (this repo's only test-rendering tool, no jsdom --
 * CLAUDE.md §4) cannot resolve a nested async component synchronously and
 * throws "component suspended" in tests. Awaiting here keeps the page
 * server-first (M9) and testable with the same tool every other page test
 * in this console uses. The Table-view fetch is never issued when the
 * Board is what's rendered (one extra call for the one surface that needs
 * it, not a wasted second round trip on every navigation).
 */
async function renderLeadsBoardSection(currentRole: Role) {
  const boardResult = await listAllLeadsForBoard();

  if (boardResult.status === "error") {
    return (
      <p
        role="alert"
        className="rounded-[14px] border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]"
      >
        {boardResult.message}
        {boardResult.correlationId ? (
          <span className="block text-xs opacity-80">Correlation ID: {boardResult.correlationId}</span>
        ) : null}
      </p>
    );
  }

  if (boardResult.items.length === 0) {
    return (
      <p role="status" className="rounded-[14px] border border-border bg-secondary p-4 text-sm text-[var(--ink-2)]">
        No leads yet -- leads captured by your chatbot will appear here.
      </p>
    );
  }

  return <LeadsBoard items={boardResult.items} revalidatePathTarget="/leads" currentRole={currentRole} />;
}

async function renderLeadsTableSection({
  page,
  stage,
  status,
  assignedAgentId,
  createdFrom,
  createdTo,
  q,
  sort,
  direction,
  leadId,
  currentParams,
  currentRole,
  currentUserId,
}: {
  page: number;
  stage: string | undefined;
  status: string | undefined;
  assignedAgentId: string | undefined;
  createdFrom: string | undefined;
  createdTo: string | undefined;
  q: string | undefined;
  sort: string | undefined;
  direction: LeadSortDirection | undefined;
  leadId: string | undefined;
  currentParams: URLSearchParams;
  currentRole: Role;
  currentUserId: string;
}) {
  const result = await listLeads({
    page, stage, status, assignedAgentId, createdFrom, createdTo, q, sort, direction,
  });

  if (result.status === "error") {
    return (
      <p
        role="alert"
        className="rounded-[14px] border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]"
      >
        {result.message}
        {result.correlationId ? (
          <span className="block text-xs opacity-80">Correlation ID: {result.correlationId}</span>
        ) : null}
      </p>
    );
  }

  if (result.items.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        <p role="status" className="rounded-[14px] border border-border bg-secondary p-4 text-sm text-[var(--ink-2)]">
          {result.total === 0 && (stage || status || assignedAgentId || createdFrom || createdTo || q) ? (
            <>
              No leads match this filter.{" "}
              <Link href="/leads" className="underline">
                Clear filter
              </Link>
            </>
          ) : result.total === 0 ? (
            "No leads yet -- leads captured by your chatbot will appear here."
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
    );
  }

  // D9: only CLIENT_ADMIN can call /admin/users. Fetch this supplementary
  // option list after the list result so an unavailable agent directory can
  // never prevent the core tenant-scoped leads table from rendering.
  const membersResult = currentRole === "CLIENT_ADMIN" ? await listMembers() : undefined;
  const agents = membersResult?.status === "ok"
    ? membersResult.items
        .filter((member) => member.role === "CLIENT_AGENT" && member.active)
        .map((member) => ({ id: member.id, label: member.name ?? member.email }))
    : undefined;
  const agentsUnavailable = membersResult?.status === "error";

  return (
    <LeadsTable
      items={result.items}
      basePath="/leads"
      currentParams={currentParams}
      selectedLeadId={leadId}
      sort={sort}
      direction={direction}
      currentRole={currentRole}
      currentUserId={currentUserId}
      agents={agents}
      agentsUnavailable={agentsUnavailable}
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
  );
}
