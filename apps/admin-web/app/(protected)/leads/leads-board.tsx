"use client";

/**
 * LeadsBoard -- SR-18 scope item 8. Replaces the "coming soon" panel
 * (`leads-view-toggle.tsx`'s M6 placeholder, deleted by this sprint -- see
 * the sprint report) with the real `PipelineBoard` wired to the LEAD funnel
 * and `PATCH /admin/leads/{lead_id}`.
 *
 * Owns the off-ramp dialog's open/close state (D4) -- `disqualified` has no
 * reason field (leads pipeline requires none) but is still confirmed, since
 * it is terminal/irreversible (SR-9.4 D5's "no reopen" reasoning carried to
 * leads by consistency + proportionality).
 *
 * SR-18 follow-up: dropping on Converted no longer sends the bare
 * `{stage:"converted"}` PATCH -- it calls the real `POST
 * /admin/leads/{lead_id}/convert` endpoint (`convertLeadToContact`, creates
 * a Contact), which is CLIENT_ADMIN-only. Two changes fall out of that:
 *
 *  1. `LEADS_PIPELINE.restrictedTargets.converted` (`lib/pipelines.ts`)
 *     gates `converted` to CLIENT_ADMIN, so `currentRole` must be passed
 *     down to `PipelineBoard` -- a CLIENT_AGENT never sees Converted light
 *     up as a drop target (same inert treatment as any other illegal
 *     column), while a CLIENT_ADMIN sees it as legal from `contacted`
 *     (the immediate prior stage), exactly like any other one-step-forward
 *     move.
 *  2. The Converted drop reuses the existing off-ramp CONFIRMATION
 *     mechanism (`onOffRampConfirm`/`OffRampDialog`) via
 *     `requiresConfirmation`, even though `converted` is not this
 *     pipeline's off-ramp (`disqualified` still is). Reasoning: converting
 *     creates a durable Contact as a side effect -- the same "significant,
 *     semi-irreversible action" category the off-ramp dialog already
 *     exists for (SR-18 D4), so a bare drag-drop committing it immediately
 *     would be a worse UX/safety trade than the extra click. A single
 *     `confirmCardId` + `confirmKind` pair of state tracks WHICH dialog
 *     (disqualify vs. convert) is open, since the two now have different
 *     copy/endpoints but share the same "confirm before commit" shape.
 */
import { useRouter } from "next/navigation";
import { useState } from "react";
import { PipelineBoard, type PipelineColumnDefinition } from "@/components/admin/pipeline-board";
import { OffRampDialog } from "@/components/admin/off-ramp-dialog";
import { LEADS_PIPELINE, isOffRamp, type PipelineRole } from "@/lib/pipelines";
import {
  convertLeadToContact,
  transitionLeadStage,
  type TransitionResult,
} from "@/app/(protected)/actions/pipeline-actions";
// Both imports MUST come from the client-safe modules, never from
// `@/lib/leads`: that module is `server-only` (it calls `adminApiFetch` ->
// `next/headers`' `cookies()` -> the JWT secret in `lib/env.ts`), so a Client
// Component importing a VALUE from it drags the whole server-only chain into
// the browser bundle and fails the build with "'server-only' cannot be
// imported from a Client Component module". `lib/leads.ts` re-exports
// `stageBadgeStyle` for its server-side callers' convenience, but this file
// must reach past that re-export to the real source. Same reasoning as
// `lead-drawer.tsx` (see `lib/leads.ts`'s header) and the same shape as the
// sibling `deals-board.tsx`, which takes its badge helper from
// `lib/deals-presentation.ts`. `LeadListItem` is a TYPE and is erased at
// compile time, but is imported from the presentation module too so this
// file has no import edge to `@/lib/leads` at all.
import { stageBadgeStyle, type LeadListItem } from "@/lib/leads-presentation";

const COLUMNS: PipelineColumnDefinition[] = [
  { key: "captured", label: "Captured" },
  { key: "qualified", label: "Qualified" },
  { key: "contacted", label: "Contacted" },
  { key: "converted", label: "Converted" },
  { key: "disqualified", label: "Disqualified" },
];

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function LeadsBoard({
  items,
  revalidatePathTarget,
  currentRole,
}: {
  items: LeadListItem[];
  revalidatePathTarget: string;
  /** The signed-in caller's role -- forwarded to `PipelineBoard` so
   * `LEADS_PIPELINE.restrictedTargets.converted` (CLIENT_ADMIN-only) can
   * exclude Converted as a drop target for a CLIENT_AGENT. */
  currentRole: PipelineRole;
}) {
  const router = useRouter();
  // Tracks which confirmation dialog is open, if any: `disqualified` (the
  // pipeline's actual off-ramp, no reason field) or `converted` (the new
  // convert-confirmation, also no reason field -- just a different endpoint
  // and copy). `null` means neither dialog is open.
  const [confirmCard, setConfirmCard] = useState<{ cardId: string; target: "disqualified" | "converted" } | null>(
    null
  );

  const cards = items.map((lead) => ({ id: lead.leadId, stage: lead.stage, lead }));

  return (
    <>
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={COLUMNS}
        cards={cards}
        currentRole={currentRole}
        emptyColumnLabel={(label) => `No leads in ${label}.`}
        renderCard={(card) => {
          const badge = stageBadgeStyle(card.lead.stage);
          return (
            <div className="flex flex-col gap-1">
              <p className="font-bold text-foreground">{card.lead.name || "Unnamed lead"}</p>
              <p className="truncate text-[11.5px] text-muted-foreground">{card.lead.email}</p>
              <div className="mt-1 flex items-center justify-between">
                <span
                  className="rounded-full px-2 py-[2px] text-[10px] font-bold"
                  style={{ background: badge.bg, color: badge.fg }}
                >
                  {badge.label}
                </span>
                <span className="text-[10.5px] text-muted-foreground">{formatDate(card.lead.createdAt)}</span>
              </div>
            </div>
          );
        }}
        onTransition={(cardId, targetStage) =>
          transitionLeadStage(cardId, targetStage, revalidatePathTarget)
        }
        // `converted` requires confirmation too (see file header) -- both
        // dialogs route through this one handler, disambiguated by target.
        requiresConfirmation={(pipeline, targetStage) =>
          isOffRamp(pipeline, targetStage) || targetStage === "converted"
        }
        onOffRampConfirm={(cardId, targetStage) => {
          setConfirmCard({
            cardId,
            target: targetStage === "converted" ? "converted" : "disqualified",
          });
        }}
      />

      <OffRampDialog
        open={confirmCard?.target === "disqualified"}
        offRampLabel="Disqualified"
        requireReason={false}
        onCancel={() => setConfirmCard(null)}
        onConfirm={async () => {
          if (!confirmCard) {
            return { status: "error", message: "No lead selected.", errorCode: "INTERNAL", correlationId: "" } as TransitionResult;
          }
          return transitionLeadStage(confirmCard.cardId, "disqualified", revalidatePathTarget);
        }}
        onSettled={(result) => {
          if (result.status === "ok") {
            setConfirmCard(null);
            router.refresh();
          }
        }}
      />

      <OffRampDialog
        open={confirmCard?.target === "converted"}
        offRampLabel="Converted"
        requireReason={false}
        onCancel={() => setConfirmCard(null)}
        onConfirm={async () => {
          if (!confirmCard) {
            return { status: "error", message: "No lead selected.", errorCode: "INTERNAL", correlationId: "" } as TransitionResult;
          }
          return convertLeadToContact(confirmCard.cardId, revalidatePathTarget);
        }}
        onSettled={(result) => {
          if (result.status === "ok") {
            setConfirmCard(null);
            router.refresh();
          }
        }}
      />
    </>
  );
}
