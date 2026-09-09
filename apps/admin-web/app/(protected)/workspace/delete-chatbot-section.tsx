"use client";

/**
 * Danger-zone "Delete this chatbot" section -- replaces the old dead
 * "Delete workspace" placeholder (see git history for `disabled-sections
 * .tsx`), which predates multi-chatbot accounts (back when one account was
 * always exactly one chatbot) and was disabled because no DELETE endpoint
 * existed. `DELETE /admin/tenants/mine` now exists (services/api/src/api/
 * admin/routes.py) and hard-deletes the CALLER'S OWN currently-active
 * chatbot, so this reframes the copy/scope to match: delete THIS chatbot,
 * not the whole account.
 *
 * Confirmation is type-the-chatbot-name-to-confirm -- new to this codebase
 * (existing patterns, `RotateKeyControl`'s inline two-step and
 * `DeactivateConfirmDialog`'s plain modal, are for reversible or
 * lower-stakes actions). This one is permanent, total data loss for a real
 * chatbot, so it gets proportionate friction. Modal shape + pessimistic UI
 * (stays open on error, shows the server's real message) mirrors
 * `components/admin/off-ramp-dialog.tsx`'s `OffRampDialog`.
 */
import { useId, useState, useTransition } from "react";
import { SoftCard } from "@/components/admin/soft-card";
import { Button } from "@/components/ui/button";
import { deleteChatbotAction } from "@/app/(protected)/workspace/delete-chatbot-actions";

export function DeleteChatbotSection({ currentName }: { currentName: string }) {
  const [open, setOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const titleId = useId();
  const descId = useId();
  const inputId = useId();

  const canSubmit = !isPending && confirmText === currentName;

  function handleOpen() {
    setConfirmText("");
    setError(null);
    setOpen(true);
  }

  function handleCancel() {
    if (isPending) return;
    setOpen(false);
  }

  function handleConfirm() {
    if (!canSubmit) return;
    setError(null);
    startTransition(async () => {
      const result = await deleteChatbotAction();
      // Only reachable on failure -- success redirects and never returns.
      setError(result.message);
    });
  }

  return (
    <>
      <SoftCard
        className="flex scroll-mt-16 flex-col gap-3 px-[22px] pb-[18px] pt-1"
        style={{ borderColor: "#e2b8b8" }}
        id="settings-danger-zone"
      >
        <h2 className="pt-4 text-[15px] font-semibold" style={{ color: "#a24b4b" }}>
          Danger zone
        </h2>
        <div className="flex items-center justify-between gap-5">
          <div>
            <p className="text-[13px] font-semibold text-foreground">Delete this chatbot</p>
            <p className="mt-[3px] max-w-md text-xs leading-[1.45] text-muted-foreground">
              Permanently remove &ldquo;{currentName}&rdquo; and all of its leads, conversations,
              knowledge base, and scheduling data. This cannot be undone. Your other chatbots (and
              your account) are not affected.
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            onClick={handleOpen}
            className="flex-none"
            style={{ borderColor: "#d99", color: "#a24b4b" }}
          >
            Delete this chatbot
          </Button>
        </div>
      </SoftCard>

      {open ? (
        <div
          role="presentation"
          className="fixed inset-0 z-[60] flex items-center justify-center bg-[rgba(25,26,23,.35)] p-4"
          onKeyDown={(event) => {
            if (event.key === "Escape" && !isPending) handleCancel();
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
              Delete &ldquo;{currentName}&rdquo;?
            </p>
            <p id={descId} className="mt-1.5 text-[12.5px] text-muted-foreground">
              This permanently deletes this chatbot and all of its data -- leads, conversations,
              knowledge base, bookings, and history. There is no way to undo this.
            </p>

            <div className="mt-3 flex flex-col gap-1.5">
              <label htmlFor={inputId} className="text-[11.5px] font-semibold text-[var(--ink-2)]">
                Type <span className="font-mono">{currentName}</span> to confirm
              </label>
              <input
                id={inputId}
                type="text"
                value={confirmText}
                onChange={(event) => setConfirmText(event.target.value)}
                disabled={isPending}
                autoComplete="off"
                className="rounded-lg border border-border bg-background p-2.5 text-[12.5px] outline-none focus-visible:border-ring"
              />
            </div>

            {error ? (
              <p
                role="alert"
                className="mt-3 rounded-lg border border-[#f6e3df] bg-[#fdf5f3] p-2 text-[11.5px] text-[var(--danger-fg)]"
              >
                {error}
              </p>
            ) : null}

            <div className="mt-4 flex justify-end gap-2">
              <Button type="button" variant="outline" size="sm" onClick={handleCancel} disabled={isPending}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={handleConfirm}
                disabled={!canSubmit}
              >
                {isPending ? "Deleting…" : "Delete this chatbot"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
