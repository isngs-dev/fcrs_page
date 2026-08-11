"use client";

/**
 * OffRampDialog -- SR-18 D4. A small confirmation dialog for the pipeline's
 * off-ramp column (`disqualified` for leads, `closed_lost` for deals).
 * Opened by `PipelineBoard.onOffRampConfirm` instead of ever submitting a
 * bare drop -- a drag gesture cannot carry the free-text `close_reason` a
 * deal's `closed_lost` transition requires (M3/D9), and a lead's
 * `disqualified` is confirmed too for consistency + proportionality (both
 * are one-way doors, SR-9.4 D5/leads pipeline no-reopen).
 *
 * `requireReason=true` (deals): the reason field is REQUIRED and the submit
 * button stays disabled until it holds non-whitespace text -- this dialog
 * will not submit an empty/placeholder reason (D4's explicitly rejected
 * alternative). `requireReason=false` (leads): no reason field at all, just
 * an irreversibility warning and a confirm button.
 *
 * Pessimistic (D5): submitting shows its own in-flight state and stays open
 * until the transition action resolves; on error the dialog shows the
 * server's real message and stays open so the user can retry or cancel --
 * never a silent close-and-hope.
 */
import { useId, useState } from "react";
import type { TransitionResult } from "@/app/(protected)/actions/pipeline-actions";

export interface OffRampDialogProps {
  open: boolean;
  offRampLabel: string;
  requireReason: boolean;
  onCancel: () => void;
  onConfirm: (closeReason: string | undefined) => Promise<TransitionResult>;
  onSettled: (result: TransitionResult) => void;
}

export function OffRampDialog({
  open,
  offRampLabel,
  requireReason,
  onCancel,
  onConfirm,
  onSettled,
}: OffRampDialogProps) {
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleId = useId();
  const descId = useId();

  if (!open) return null;

  const canSubmit = !pending && (!requireReason || reason.trim().length > 0);

  async function handleConfirm() {
    if (!canSubmit) return;
    setPending(true);
    setError(null);
    const result = await onConfirm(requireReason ? reason.trim() : undefined);
    setPending(false);
    if (result.status === "error") {
      setError(result.message);
    }
    onSettled(result);
  }

  return (
    <div
      role="presentation"
      className="fixed inset-0 z-[60] flex items-center justify-center bg-[rgba(25,26,23,.35)] p-4"
      onKeyDown={(event) => {
        if (event.key === "Escape" && !pending) onCancel();
      }}
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        className="w-full max-w-sm rounded-[14px] border border-border bg-card p-5 shadow-[0_1px_2px_rgba(28,27,25,.03)]"
      >
        <p id={titleId} className="text-sm font-bold text-foreground">
          Move to {offRampLabel}?
        </p>
        <p id={descId} className="mt-1.5 text-[12.5px] text-muted-foreground">
          This is a one-way move. Once confirmed, this record cannot be reopened or moved again.
        </p>

        {requireReason ? (
          <div className="mt-3 flex flex-col gap-1.5">
            <label htmlFor="off-ramp-reason" className="text-[11.5px] font-semibold text-[var(--ink-2)]">
              Reason (required)
            </label>
            <textarea
              id="off-ramp-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              disabled={pending}
              rows={3}
              className="rounded-lg border border-border bg-background p-2.5 text-[12.5px] outline-none focus-visible:border-ring"
              placeholder="Why was this deal lost?"
            />
          </div>
        ) : null}

        {error ? (
          <p role="alert" className="mt-3 rounded-lg border border-[#f6e3df] bg-[#fdf5f3] p-2 text-[11.5px] text-[var(--danger-fg)]">
            {error}
          </p>
        ) : null}

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={pending}
            className="min-h-9 rounded-[9px] border border-border bg-card px-3.5 text-[12.5px] font-semibold text-[var(--ink-2)] hover:bg-secondary"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!canSubmit}
            className="min-h-9 rounded-[9px] bg-primary px-3.5 text-[12.5px] font-semibold text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            {pending ? "Moving…" : `Confirm move to ${offRampLabel}`}
          </button>
        </div>
      </div>
    </div>
  );
}
