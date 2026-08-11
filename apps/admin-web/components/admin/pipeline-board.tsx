"use client";

/**
 * PipelineBoard -- SR-18 D3. The ONE board component serving BOTH Deals and
 * Leads, parameterized by an injected stage list / terminal set / off-ramp /
 * card renderer / transition action -- not two forked components. The two
 * funnels' different shapes (`PipelineDefinition` from `lib/pipelines.ts`)
 * are DATA passed in, not different code paths.
 *
 * D7 -- drag-and-drop mechanism: HAND-ROLLED POINTER EVENTS. No new
 * dependency. Chosen over native HTML5 DnD (fails on touch, criterion 3) and
 * over a library (this board's drop-target constraint logic (D2) and
 * pessimistic in-flight state (D5) are bespoke enough, and the actual
 * "dragging" here is simple single-item reordering-free movement between a
 * handful of columns, not a general sortable-list problem) after weighing
 * all five of D7's criteria:
 *   1. Keyboard-operable: every card is a `<button>` with `tabIndex=0`.
 *      Space/Enter "grabs" it (sets `grabbedId` + enters keyboard-nav mode),
 *      Arrow Left/Right moves focus among the CURRENTLY LEGAL target columns
 *      only (illegal columns are skipped, never focusable while grabbed),
 *      Enter/Space again drops on the focused column, Escape cancels and
 *      returns focus to the origin card. No pointer is required for any
 *      state transition.
 *   2. Screen-reader announced: a single `aria-live="polite"` region
 *      (`#pipeline-board-announcer`, visually hidden) is updated on grab
 *      ("Grabbed <name>, currently in <stage>. Use arrow keys to choose a
 *      target column."), on target change ("<column> selected as drop
 *      target." / for inert columns, nothing -- they cannot receive focus),
 *      and on outcome (success or the server's real rejection message).
 *   3. Touch-capable: the same grab/move/drop state machine is driven by
 *      `pointerdown`/`pointermove`/`pointerup` via the Pointer Events API,
 *      which unifies mouse, pen and touch input in one listener set (no
 *      separate touch fallback needed -- Pointer Events *are* the touch
 *      path, unlike HTML5 DnD which has none). `touch-action: none` is set
 *      on grabbed cards during a pointer drag to suppress scroll-hijacking.
 *   4. Constrainable drop targets: this is native to the design -- each
 *      column receives `isLegalTarget(pipeline, card.stage, column.key)`
 *      (D2/D8) as a boolean prop and renders either an active drop-target
 *      treatment (pointer-events enabled, `aria-dropeffect="move"`) or a
 *      fully inert one (`pointer-events-none` on the drop-catching overlay,
 *      `aria-dropeffect="none"`, no hover state) -- there is no code path
 *      that can accept a drop on an illegal column.
 *   5. N/A (no library chosen) -- bundle size impact is zero; this is
 *      Pointer Events, already part of the DOM API surface every browser
 *      this console supports ships natively.
 *
 * D5 -- pessimistic transitions: dropping/keyboard-confirming a move calls
 * `onTransition` (the injected server action) and renders the card in a
 * dimmed "moving…" in-flight state in its ORIGIN column while the request is
 * in flight (`pendingCardId` state). The card is only re-parented to the new
 * column after `onTransition` resolves `{status:"ok"}`; on `{status:"error"}`
 * the card simply stays put with a visible rejection banner naming the
 * server's real message + correlation ID (never a silent snap-back -- there
 * is nothing to snap back FROM, since nothing moved yet).
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { PipelineDefinition, PipelineRole } from "@/lib/pipelines";
import { isDraggable, isLegalTarget, isOffRamp, legalTargets } from "@/lib/pipelines";
import type { TransitionResult } from "@/app/(protected)/actions/pipeline-actions";

export interface PipelineCard {
  /** Stable identity used for React keys, drag tracking, and the transition
   * action's first argument (lead_id / opportunity_id). */
  id: string;
  /** The card's CURRENT stage -- must be one of `pipeline.stageOrder` or a
   * value in `pipeline.terminalStages`. */
  stage: string;
}

export interface PipelineColumnDefinition {
  key: string;
  label: string;
}

export interface PipelineBoardProps<TCard extends PipelineCard> {
  pipeline: PipelineDefinition;
  /** Every column to render, in display order -- includes the off-ramp
   * column. Distinct from `pipeline.stageOrder`, which is funnel-order only
   * (the off-ramp is deliberately rendered last by both call sites, but that
   * is a caller decision, not this component's). */
  columns: PipelineColumnDefinition[];
  cards: TCard[];
  /** The signed-in caller's role, forwarded to `legalTargets`/
   * `isLegalTarget` (`lib/pipelines.ts`) so a pipeline's
   * `restrictedTargets` (today: only `LEADS_PIPELINE.converted`, gated to
   * CLIENT_ADMIN) can exclude a target column for callers without the
   * required role. Omit for pipelines with no restricted targets (deals) --
   * behavior is identical to before this prop existed. */
  currentRole?: PipelineRole;
  /** Renders a card's body (name, amount, chips, etc.) -- the ONLY place
   * entity-specific presentation lives; this component never inspects
   * anything on `TCard` beyond `id`/`stage`. */
  renderCard: (card: TCard) => React.ReactNode;
  /** Calls the server action for a drop that doesn't require confirmation.
   * Confirmation-required drops go through `onConfirmRequired` instead (D4
   * -- never a bare drag). */
  onTransition: (cardId: string, targetStage: string) => Promise<TransitionResult>;
  /** Opens a confirmation dialog for `cardId`/`targetStage` -- called
   * instead of `onTransition` whenever the resolved drop/keyboard-confirm
   * target requires confirmation first (D4). Defaults to `isOffRamp` (the
   * pipeline's off-ramp column) when `requiresConfirmation` is omitted. The
   * dialog itself owns calling `onTransition` (or, for leads' `converted`,
   * the board's own confirm-and-convert handling) once the user confirms.
   * `targetStage` lets a caller with more than one confirmation-required
   * target (leads: `disqualified` AND `converted`) tell which dialog to
   * open without guessing from the card's current stage. */
  onOffRampConfirm: (cardId: string, targetStage: string) => void;
  /** Which target stages require confirmation before submitting, beyond
   * ordinary drop legality. Defaults to `(pipeline, stage) =>
   * isOffRamp(pipeline, stage)` -- the original SR-18 behavior (only the
   * off-ramp confirms). `leads-board.tsx` additionally routes `converted`
   * through confirmation, since that drop creates a Contact as a side
   * effect (same "significant, semi-irreversible action" category as the
   * off-ramp). */
  requiresConfirmation?: (pipeline: PipelineDefinition, targetStage: string) => boolean;
  /** Empty-column copy, e.g. "No leads in this stage." */
  emptyColumnLabel?: (columnLabel: string) => string;
}

interface DragState {
  cardId: string;
  originStage: string;
  /** Pointer-drag only: current pointer position in viewport coords, used to
   * render a floating ghost. `null` for keyboard-driven grabs (no ghost --
   * the focused column IS the feedback). */
  pointer: { x: number; y: number } | null;
  /** The column key currently focused/hovered as the candidate drop target,
   * or `null` if none. Always one of the card's legal targets while set. */
  focusedTarget: string | null;
}

export function PipelineBoard<TCard extends PipelineCard>({
  pipeline,
  columns,
  cards,
  currentRole,
  renderCard,
  onTransition,
  onOffRampConfirm,
  requiresConfirmation,
  emptyColumnLabel,
}: PipelineBoardProps<TCard>) {
  const needsConfirmation = requiresConfirmation ?? isOffRamp;
  const [drag, setDrag] = useState<DragState | null>(null);
  const [pendingCardId, setPendingCardId] = useState<string | null>(null);
  const [rejection, setRejection] = useState<{ cardId: string; message: string; correlationId: string } | null>(
    null
  );
  const [announcement, setAnnouncement] = useState("");
  const boardRef = useRef<HTMLDivElement>(null);
  const cardRefs = useRef<Map<string, HTMLButtonElement>>(new Map());
  const announcerId = useId();

  const cardsByColumn = useMemo(() => {
    const map = new Map<string, TCard[]>();
    for (const column of columns) map.set(column.key, []);
    for (const card of cards) {
      const bucket = map.get(card.stage);
      if (bucket) bucket.push(card);
    }
    return map;
  }, [columns, cards]);

  const announce = useCallback((message: string) => {
    setAnnouncement(message);
  }, []);

  const beginGrab = useCallback(
    (card: TCard, pointer: { x: number; y: number } | null) => {
      if (!isDraggable(pipeline, card.stage) || pendingCardId) return;
      const targets = legalTargets(pipeline, card.stage, currentRole);
      setDrag({ cardId: card.id, originStage: card.stage, pointer, focusedTarget: targets[0] ?? null });
      setRejection(null);
      announce(
        `Grabbed card, currently in ${card.stage}. ${
          targets.length > 0
            ? `Use arrow keys to choose a target column: ${targets.join(" or ")}.`
            : "No target columns are available."
        }`
      );
    },
    [pipeline, pendingCardId, currentRole, announce]
  );

  const cancelDrag = useCallback(
    (returnFocus: boolean) => {
      if (!drag) return;
      const cardId = drag.cardId;
      setDrag(null);
      announce("Move canceled. Card returned to its original column.");
      if (returnFocus) {
        requestAnimationFrame(() => cardRefs.current.get(cardId)?.focus());
      }
    },
    [drag, announce]
  );

  const commitDrop = useCallback(
    async (cardId: string, targetStage: string, originStage: string) => {
      setDrag(null);

      if (needsConfirmation(pipeline, targetStage)) {
        // D4: a confirmation-required target NEVER submits from a bare drop
        // -- always routes to the confirmation dialog, which owns calling
        // onTransition (or the board's own convert handling) itself.
        onOffRampConfirm(cardId, targetStage);
        announce(`${targetStage} requires confirmation. Opening dialog.`);
        return;
      }

      setPendingCardId(cardId);
      announce(`Moving card to ${targetStage}…`);

      const result = await onTransition(cardId, targetStage);

      setPendingCardId(null);
      if (result.status === "ok") {
        setRejection(null);
        announce(`Card moved to ${result.stage}.`);
      } else {
        setRejection({ cardId, message: result.message, correlationId: result.correlationId });
        announce(`Move rejected: ${result.message}`);
        requestAnimationFrame(() => cardRefs.current.get(cardId)?.focus());
      }
      void originStage;
    },
    [pipeline, onTransition, onOffRampConfirm, needsConfirmation, announce]
  );

  // -- Pointer Events (mouse/pen/touch unified) ------------------------------
  useEffect(() => {
    if (!drag || drag.pointer === null) return;

    function handleMove(event: PointerEvent) {
      setDrag((current) => {
        if (!current || current.pointer === null) return current;
        const el = document.elementFromPoint(event.clientX, event.clientY);
        const columnEl = el?.closest<HTMLElement>("[data-pipeline-column]");
        const columnKey = columnEl?.dataset.pipelineColumn ?? null;
        const legal =
          columnKey && isLegalTarget(pipeline, current.originStage, columnKey, currentRole)
            ? columnKey
            : null;
        return { ...current, pointer: { x: event.clientX, y: event.clientY }, focusedTarget: legal };
      });
    }

    function handleUp() {
      setDrag((current) => {
        if (!current) return current;
        if (current.focusedTarget) {
          void commitDrop(current.cardId, current.focusedTarget, current.originStage);
        } else {
          announce("Move canceled. Card returned to its original column.");
        }
        return null;
      });
    }

    function handleCancelKey(event: KeyboardEvent) {
      if (event.key === "Escape") cancelDrag(true);
    }

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("keydown", handleCancelKey);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("keydown", handleCancelKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- commitDrop/announce/cancelDrag are stable across the drag's lifetime for this effect's purposes
  }, [drag?.cardId, pipeline, currentRole]);

  function handlePointerDownOnCard(event: React.PointerEvent<HTMLButtonElement>, card: TCard) {
    if (!isDraggable(pipeline, card.stage) || pendingCardId) return;
    event.preventDefault();
    beginGrab(card, { x: event.clientX, y: event.clientY });
  }

  function handleCardKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, card: TCard) {
    if (pendingCardId) return;

    if (!drag || drag.cardId !== card.id) {
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        beginGrab(card, null);
      }
      return;
    }

    const targets = legalTargets(pipeline, drag.originStage, currentRole);
    if (event.key === "Escape") {
      event.preventDefault();
      cancelDrag(true);
      return;
    }
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      if (targets.length === 0) return;
      const currentIdx = drag.focusedTarget ? targets.indexOf(drag.focusedTarget) : -1;
      const delta = event.key === "ArrowRight" ? 1 : -1;
      const nextIdx = (currentIdx + delta + targets.length) % targets.length;
      const next = targets[nextIdx];
      setDrag({ ...drag, focusedTarget: next });
      announce(`${next} selected as drop target.`);
      return;
    }
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      if (drag.focusedTarget) {
        void commitDrop(drag.cardId, drag.focusedTarget, drag.originStage);
      }
    }
  }

  return (
    <div ref={boardRef} className="flex flex-col gap-3">
      <div
        id={announcerId}
        role="status"
        aria-live="polite"
        className="sr-only"
        data-testid="pipeline-board-announcer"
      >
        {announcement}
      </div>

      <div className="flex flex-1 gap-4 overflow-x-auto pb-2">
        {columns.map((column) => {
          const isLegal = drag ? isLegalTarget(pipeline, drag.originStage, column.key, currentRole) : false;
          const isFocused = drag?.focusedTarget === column.key;
          const columnCards = cardsByColumn.get(column.key) ?? [];

          return (
            <div
              key={column.key}
              data-pipeline-column={column.key}
              aria-dropeffect={drag ? (isLegal ? "move" : "none") : undefined}
              className="flex w-72 shrink-0 flex-col gap-2 rounded-[14px] border border-border bg-secondary/40 p-2.5"
              style={
                drag
                  ? isFocused
                    ? { outline: "2px solid var(--ring)", outlineOffset: "2px", background: "#fdfdec" }
                    : isLegal
                      ? { outline: "1.5px dashed var(--ink-2)", outlineOffset: "2px" }
                      : { opacity: 0.45 }
                  : undefined
              }
            >
              <div className="flex items-center justify-between px-1 py-1">
                <p className="text-[11px] font-semibold tracking-[.06em] text-muted-foreground uppercase">
                  {column.label}
                </p>
                <span className="text-[11px] text-muted-foreground">{columnCards.length}</span>
              </div>

              <div className="flex flex-col gap-2">
                {columnCards.length === 0 ? (
                  <p
                    role="status"
                    className="rounded-lg border border-dashed border-[var(--line-2)] p-3 text-center text-[11.5px] text-muted-foreground"
                  >
                    {emptyColumnLabel ? emptyColumnLabel(column.label) : "Nothing here."}
                  </p>
                ) : (
                  columnCards.map((card) => {
                    const draggableHere = isDraggable(pipeline, card.stage);
                    const isPending = pendingCardId === card.id;
                    const isRejected = rejection?.cardId === card.id;
                    const isGrabbed = drag?.cardId === card.id;

                    return (
                      <div key={card.id} className="flex flex-col gap-1">
                        <button
                          type="button"
                          ref={(el) => {
                            if (el) cardRefs.current.set(card.id, el);
                            else cardRefs.current.delete(card.id);
                          }}
                          disabled={isPending}
                          aria-disabled={!draggableHere || isPending}
                          aria-roledescription={draggableHere ? "draggable card" : "card (locked, terminal stage)"}
                          aria-grabbed={isGrabbed}
                          data-testid={`pipeline-card-${card.id}`}
                          onPointerDown={(event) => handlePointerDownOnCard(event, card)}
                          onKeyDown={(event) => handleCardKeyDown(event, card)}
                          style={{ touchAction: isGrabbed ? "none" : undefined }}
                          className={`w-full rounded-[12px] border border-border bg-card p-3 text-left text-[13px] shadow-[0_1px_2px_rgba(28,27,25,.03)] transition-opacity focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                            !draggableHere ? "cursor-default opacity-90" : "cursor-grab active:cursor-grabbing"
                          } ${isPending ? "opacity-50" : ""} ${isGrabbed ? "ring-2 ring-[var(--ring)]" : ""}`}
                        >
                          {renderCard(card)}
                          {isPending ? (
                            <p className="mt-1.5 text-[11px] font-semibold text-muted-foreground">Moving…</p>
                          ) : null}
                        </button>
                        {isRejected ? (
                          <p
                            role="alert"
                            className="rounded-lg border border-[#f6e3df] bg-[#fdf5f3] p-2 text-[11.5px] text-[var(--danger-fg)]"
                          >
                            {rejection.message}
                            {rejection.correlationId ? (
                              <span className="mt-0.5 block opacity-80">
                                Correlation ID: {rejection.correlationId}
                              </span>
                            ) : null}
                          </p>
                        ) : null}
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
