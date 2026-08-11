"use client";

/**
 * Thin client adapter wiring a Deal (Opportunity) onto the shared
 * `RecordDrawer` (SR-17 D3, this sprint's third consumer per the sprint
 * report). Mirrors `contact-drawer.tsx`'s URL open/close shape exactly.
 *
 * IMPORTANT (scope item 7, stated explicitly per the sprint instructions):
 * the timeline endpoints (`lib/timeline.ts`) support ONLY
 * `kind: "contact" | "lead"` -- verified live, there is no opportunity item
 * kind anywhere in `services/api/src/api/timeline/`. SR-9.4's own "Future
 * work" section already names this gap. This drawer therefore does NOT call
 * `getContactTimeline`/`getLeadTimeline` for a deal and does NOT fabricate a
 * deal event feed -- it renders the deal's own fields as the summary/detail
 * content and passes `RecordDrawer` an explicit "ok, empty, no loadOlder"
 * timeline result carrying one honest notice item instead of a real
 * activity log. When SR-9.3 adds an opportunity timeline item kind, this
 * drawer's only needed change is swapping that stub for a real fetch.
 */
import { useCallback } from "react";
import { useRouter } from "next/navigation";
import type { DealDetailResult } from "@/lib/deals";
import type { TimelineFetchResult } from "@/lib/timeline";
import { RecordDrawer, type RecordDrawerSubject } from "@/components/admin/record-drawer";

function formatAmount(amount: string | null, currency: string): string {
  if (amount === null) return "Not quoted";
  return `${amount} ${currency}`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function DealDrawer({
  opportunityId,
  detailResult,
  basePath,
}: {
  opportunityId: string;
  detailResult: DealDetailResult;
  basePath: string;
}) {
  const router = useRouter();

  const close = useCallback(() => {
    router.push(basePath, { scroll: false });
  }, [router, basePath]);

  if (detailResult.status === "error") {
    return (
      <RecordDrawer
        subject={{ kind: "lead", id: opportunityId, name: null, secondary: null }}
        result={{ status: "error", message: detailResult.message, correlationId: detailResult.correlationId }}
        onClose={close}
        loadOlderHref={null}
      />
    );
  }

  const { deal } = detailResult;

  const subject: RecordDrawerSubject = {
    // `RecordDrawerSubject.kind` is a fixed union of "contact"|"lead" (SR-17
    // D3) -- there is no third "deal" kind yet, and adding one is a shared-
    // component change out of this sprint's scope (D3 says the funnels'
    // shapes are DATA into one board component; the drawer's subject kind
    // union is a separate, smaller surface not touched here). "lead" is
    // used purely for the header's avatar-initials styling; nothing in
    // `RecordDrawer`'s rendering branches on kind beyond that and the
    // "View accounts" link, which this subject supplies directly below.
    kind: "lead",
    id: opportunityId,
    name: deal.name,
    secondary: `${formatAmount(deal.amount, deal.currency)} · ${deal.stage}`,
    summary: `Win probability: ${deal.winProbability}% (derived, not editable) · Expected close: ${formatDate(
      deal.expectedCloseDate
    )}${deal.closeReason ? ` · Close reason: ${deal.closeReason}` : ""}`,
    accountId: deal.accountId,
  };

  // Honest stand-in (see header comment): no opportunity timeline endpoint
  // exists yet, so this is NOT a fabricated activity feed -- it is a single
  // explanatory item, structurally valid `TimelineFetchResult` so
  // `RecordDrawer` renders it through its normal (non-error, non-degraded)
  // path rather than a special case, but its `data` payload is proof this
  // list wasn't scraped from misapplied lead/contact history.
  const stubTimeline: TimelineFetchResult = {
    status: "ok",
    data: {
      subject: { kind: "lead", id: opportunityId, convertedToContactId: null },
      degraded: false,
      sources: {},
      items: [
        {
          kind: "deal_history_unavailable",
          occurredAt: deal.createdAt,
          id: `${opportunityId}-no-timeline`,
          data: { note: "Deal activity history is not tracked yet (planned in a future sprint)." },
        },
      ],
      nextBefore: null,
    },
  };

  return <RecordDrawer subject={subject} result={stubTimeline} onClose={close} loadOlderHref={null} />;
}
