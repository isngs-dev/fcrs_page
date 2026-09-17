"use client";

/**
 * Delete-a-knowledge-doc control for the client-facing `/knowledge` page.
 * Mirrors `clients/[tenantId]/rotate-key-control.tsx`'s simplest inline
 * two-step confirm pattern (button -> "Continue?" + Cancel/Delete) --
 * proportionate weight for deleting one document (real but recoverable by
 * re-uploading), unlike the whole-chatbot type-to-confirm dialog.
 *
 * Rendered as a small client child inside `doc-list.tsx`'s otherwise
 * server-rendered card -- the rest of that list stays server-only; only
 * this control ships client JS. On success, `deleteKnowledgeDocAction`
 * already calls `revalidatePath("/knowledge")` server-side, so the deleted
 * row disappears on the next render without any local list-mutation code
 * here (confirmed, not optimistic -- this app's established pattern).
 */
import { useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { deleteKnowledgeDocAction } from "@/app/(protected)/knowledge/actions";

export function DeleteDocButton({ docId, label }: { docId: string; label: string }) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  function handleConfirm() {
    setError(null);
    startTransition(async () => {
      const result = await deleteKnowledgeDocAction(docId);
      if (result.status === "error") {
        setError(result.message);
        setConfirming(false);
      }
      // On success the row is gone after revalidation -- nothing more to do.
    });
  }

  if (!confirming) {
    return (
      <div className="flex flex-col items-start gap-1">
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="text-[12px] font-semibold text-destructive underline underline-offset-2 hover:no-underline"
        >
          Delete
        </button>
        {error ? (
          <p role="alert" className="text-[11.5px] text-destructive">
            {error}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-[11.5px] text-muted-foreground">
        Permanently delete &ldquo;{label}&rdquo;?
      </span>
      <Button
        type="button"
        variant="destructive"
        size="sm"
        disabled={isPending}
        onClick={handleConfirm}
      >
        {isPending ? "Deleting…" : "Delete"}
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled={isPending}
        onClick={() => setConfirming(false)}
      >
        Cancel
      </Button>
    </div>
  );
}
