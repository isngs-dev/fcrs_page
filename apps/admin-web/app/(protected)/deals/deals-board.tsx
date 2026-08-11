"use client";

/**
 * DealsBoard -- SR-18 scope items 1-6. Wires `PipelineBoard` to the
 * OPPORTUNITY funnel and `POST /admin/opportunities/{id}/stage`. Unlike
 * `leads-board.tsx`, the off-ramp (`closed_lost`) REQUIRES a non-empty
 * `close_reason` (D4/M3/D9) -- `OffRampDialog`'s `requireReason` is `true`
 * here and `false` there; this is the only difference between the two
 * board wrappers beyond which pipeline/action they inject, which is exactly
 * D3's point (the funnels' differences are DATA, not code paths).
 */
import { useRouter } from "next/navigation";
import { useState } from "react";
import { PipelineBoard, type PipelineColumnDefinition } from "@/components/admin/pipeline-board";
import { OffRampDialog } from "@/components/admin/off-ramp-dialog";
import { OPPORTUNITIES_PIPELINE } from "@/lib/pipelines";
import { transitionDealStage, type TransitionResult } from "@/app/(protected)/actions/pipeline-actions";
import type { Deal } from "@/lib/deals";
import { dealStageBadgeStyle, formatDealAmount, formatDealDate } from "@/lib/deals-presentation";

const COLUMNS: PipelineColumnDefinition[] = [
  { key: "prospecting", label: "Prospecting" },
  { key: "qualification", label: "Qualification" },
  { key: "proposal", label: "Proposal" },
  { key: "negotiation", label: "Negotiation" },
  { key: "closed_won", label: "Closed Won" },
  { key: "closed_lost", label: "Closed Lost" },
];

export function DealsBoard({ items, revalidatePathTarget }: { items: Deal[]; revalidatePathTarget: string }) {
  const router = useRouter();
  const [offRampCardId, setOffRampCardId] = useState<string | null>(null);

  const cards = items.map((deal) => ({ id: deal.opportunityId, stage: deal.stage, deal }));

  return (
    <>
      <PipelineBoard
        pipeline={OPPORTUNITIES_PIPELINE}
        columns={COLUMNS}
        cards={cards}
        emptyColumnLabel={(label) => `No deals in ${label}.`}
        renderCard={(card) => {
          const badge = dealStageBadgeStyle(card.deal.stage);
          return (
            <div className="flex flex-col gap-1">
              <p className="font-bold text-foreground">{card.deal.name}</p>
              {/* D6: honest money -- never $0 for a null amount, always the
                  row's own currency, never a computed total. */}
              <p className="text-[12px] font-semibold text-[var(--ink-2)]">
                {formatDealAmount(card.deal.amount, card.deal.currency)}
              </p>
              <div className="mt-1 flex items-center justify-between">
                <span
                  className="rounded-full px-2 py-[2px] text-[10px] font-bold"
                  style={{ background: badge.bg, color: badge.fg }}
                >
                  {badge.label}
                </span>
                <span className="text-[10.5px] text-muted-foreground">
                  {card.deal.winProbability}% win
                </span>
              </div>
              <p className="text-[10.5px] text-muted-foreground">
                Close: {formatDealDate(card.deal.expectedCloseDate)}
              </p>
            </div>
          );
        }}
        onTransition={(cardId, targetStage) =>
          transitionDealStage(cardId, targetStage, undefined, revalidatePathTarget)
        }
        // Deals has only one confirmation-required target (`closed_lost`,
        // the actual off-ramp), so `targetStage` is unused here -- the
        // default `requiresConfirmation` (`isOffRamp`) is unchanged.
        onOffRampConfirm={(cardId) => setOffRampCardId(cardId)}
      />

      <OffRampDialog
        open={offRampCardId !== null}
        offRampLabel="Closed Lost"
        requireReason
        onCancel={() => setOffRampCardId(null)}
        onConfirm={async (closeReason) => {
          if (!offRampCardId) {
            return { status: "error", message: "No deal selected.", errorCode: "INTERNAL", correlationId: "" } as TransitionResult;
          }
          return transitionDealStage(offRampCardId, "closed_lost", closeReason, revalidatePathTarget);
        }}
        onSettled={(result) => {
          if (result.status === "ok") {
            setOffRampCardId(null);
            router.refresh();
          }
        }}
      />
    </>
  );
}
