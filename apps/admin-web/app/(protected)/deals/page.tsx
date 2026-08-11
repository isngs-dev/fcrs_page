/**
 * Deals page -- SR-18 D1, replaces SR-15's RBAC-gated placeholder stub
 * entirely (that stub's body is deleted; its RBAC gate -- CLIENT_ADMIN +
 * CLIENT_AGENT, matching `/leads` -- is proven again here unchanged).
 *
 * The mockup has no Deals screen at all (M7), so this page is DESIGNED, not
 * transcribed: it reuses `/leads`'s exact shape -- a `Table|Board`
 * segmented toggle over the same `TableCard` primitives and the same
 * `PipelineBoard` component, plus SR-17's shared `record-drawer.tsx` for
 * detail. Columns per D1: name, contact, account, amount + currency (D6),
 * stage chip, expected close date, owner.
 *
 * `POST /admin/opportunities` (create) is CLIENT_ADMIN + CLIENT_AGENT
 * (SR-9.4 D8, unlike SR-17's admin-only contacts) -- both roles could write
 * one, but a full create form (contact lookup, optional account
 * resolution) is a materially separate feature not named in this sprint's
 * numbered scope list (items 1-11); "Add deal" is rendered as an honest,
 * clearly-labeled disabled affordance for now, mirroring the existing CSV
 * export pattern elsewhere in this console, rather than fabricating a
 * partial create flow (CLAUDE.md §3, no-silent-fallback applies to
 * unfinished affordances too).
 *
 * Board view (`?view=board`) fetches all deals unfiltered, same posture as
 * `leads/page.tsx`'s `LeadsBoardSection` -- SR-18 D2's constrained board
 * needs the whole pipeline visible at once, not one filtered/paginated
 * slice.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { listAllDealsForBoard, listDeals } from "@/lib/deals";
import { DealsTable } from "@/app/(protected)/deals/deals-table";
import { DealsBoard } from "@/app/(protected)/deals/deals-board";
import { DealsFilter } from "@/app/(protected)/deals/deals-filter";
import { DealsViewToggle } from "@/app/(protected)/deals/deals-view-toggle";
import { DealDrawerContainer } from "@/app/(protected)/deals/deal-drawer-container";
import { PagePagination } from "@/components/admin/page-pagination";
import { SoftCard } from "@/components/admin/soft-card";

interface DealsPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function pageHref(page: number, stage: string | undefined, view: "table" | "board"): string {
  const query = new URLSearchParams();
  if (page > 1) query.set("page", String(page));
  if (stage) query.set("stage", stage);
  if (view === "board") query.set("view", view);
  const qs = query.toString();
  return qs ? `/deals?${qs}` : "/deals";
}

export default async function DealsPage({ searchParams }: DealsPageProps) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const params = await searchParams;
  const stage = firstValue(params.stage);
  const rawPage = Number.parseInt(firstValue(params.page) ?? "1", 10);
  const page = Number.isFinite(rawPage) && rawPage >= 1 ? rawPage : 1;
  const view = firstValue(params.view) === "board" ? "board" : "table";
  const dealId = firstValue(params.deal);

  const currentParams = new URLSearchParams();
  if (page > 1) currentParams.set("page", String(page));
  if (stage) currentParams.set("stage", stage);
  if (view === "board") currentParams.set("view", view);

  return (
    <div className="flex flex-1 flex-col gap-4 p-6 lg:p-8">
      <div className="flex flex-wrap items-center gap-3.5">
        <div>
          <h1 className="text-xl font-bold text-foreground">Deals</h1>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            Track opportunities as they move toward close.
          </p>
        </div>
        <DealsViewToggle view={view} basePath="/deals" currentParams={currentParams} />
        <div className="ml-auto flex items-center gap-2.5">
          <DealsFilter currentStage={stage} />
          <span
            title="Creating a deal from the console needs a contact/account lookup this sprint doesn't build -- deals are created via the API today."
            aria-disabled="true"
            className="min-h-9 cursor-not-allowed rounded-[9px] bg-primary/50 px-3.5 py-2 text-[12.5px] font-semibold whitespace-nowrap text-primary-foreground/80"
          >
            + Add deal
          </span>
        </div>
      </div>

      {view === "board"
        ? await renderDealsBoardSection()
        : await renderDealsTableSection({ page, stage, view, dealId, currentParams })}

      {dealId ? <DealDrawerContainer opportunityId={dealId} basePath="/deals" /> : null}
    </div>
  );
}

/**
 * Rendered directly (`await renderDealsBoardSection()` inside `DealsPage`'s
 * own JSX), not as `<DealsBoardSection />`, so this branch resolves BEFORE
 * `DealsPage` returns -- an async component embedded as a JSX child would
 * still work in the real Next.js RSC runtime, but `react-dom/server`'s
 * `renderToStaticMarkup` (this repo's only test-rendering tool, no jsdom --
 * CLAUDE.md §4) cannot resolve a nested async component synchronously and
 * throws "component suspended" in tests. Awaiting here keeps the page
 * server-first (M9) and testable with the same tool every other page test
 * in this console already uses.
 */
async function renderDealsBoardSection() {
  const boardResult = await listAllDealsForBoard();
  // (see lib/deals.ts's `listAllDealsForBoard` for the fetch/mapping this
  // section renders)

  if (boardResult.status === "error") {
    return (
      <p role="alert" className="rounded-[14px] border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]">
        {boardResult.message}
        {boardResult.correlationId ? (
          <span className="block text-xs opacity-80">Correlation ID: {boardResult.correlationId}</span>
        ) : null}
      </p>
    );
  }

  if (boardResult.items.length === 0) {
    return (
      <SoftCard className="flex flex-col items-center justify-center gap-2 p-12 text-center">
        <p className="text-sm font-semibold text-[var(--ink-2)]">No deals yet</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Deals created against this tenant&apos;s pipeline will appear here.
        </p>
      </SoftCard>
    );
  }

  return <DealsBoard items={boardResult.items} revalidatePathTarget="/deals" />;
}

async function renderDealsTableSection({
  page,
  stage,
  view,
  dealId,
  currentParams,
}: {
  page: number;
  stage: string | undefined;
  view: "table" | "board";
  dealId: string | undefined;
  currentParams: URLSearchParams;
}) {
  const result = await listDeals({ page, stage });

  if (result.status === "error") {
    return (
      <p role="alert" className="rounded-[14px] border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]">
        {result.message}
        {result.correlationId ? (
          <span className="block text-xs opacity-80">Correlation ID: {result.correlationId}</span>
        ) : null}
      </p>
    );
  }

  if (result.items.length === 0) {
    return (
      <SoftCard className="flex flex-col items-center justify-center gap-2 p-12 text-center">
        <p className="text-sm font-semibold text-[var(--ink-2)]">
          {result.total === 0 && stage ? "No deals match this filter" : result.total === 0 ? "No deals yet" : "No deals on this page"}
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          {result.total === 0 && stage ? (
            <>
              No deals match this filter.{" "}
              <Link href="/deals" className="underline">
                Clear filter
              </Link>
            </>
          ) : result.total === 0 ? (
            "Deals created against this tenant's pipeline will appear here."
          ) : (
            "Try an earlier page."
          )}
        </p>
        {result.total > 0 && result.offset > 0 ? (
          <Link href={pageHref(page - 1, stage, view)} className="text-sm underline">
            Previous
          </Link>
        ) : null}
      </SoftCard>
    );
  }

  return (
    <>
      <DealsTable items={result.items} basePath="/deals" currentParams={currentParams} selectedDealId={dealId} />
      <PagePagination
        page={page}
        hasPrevious={result.offset > 0}
        hasNext={result.offset + result.limit < result.total}
        prevHref={pageHref(page - 1, stage, view)}
        nextHref={pageHref(page + 1, stage, view)}
        rangeLabel={`Showing ${result.offset + 1}–${result.offset + result.items.length} of ${result.total}`}
      />
    </>
  );
}
