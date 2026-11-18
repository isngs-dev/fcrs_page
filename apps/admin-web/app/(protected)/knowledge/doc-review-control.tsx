"use client";

/**
 * Plan 2.6: a knowledge document's category and whether the agent uses it, on the
 * client-facing `/knowledge` list (CLIENT_ADMIN -- the API is the real gate). Confirmed, not
 * optimistic: `reviewKnowledgeDocAction` revalidates `/knowledge` and the row re-renders.
 */
import { useState, useTransition } from "react";
import { KNOWLEDGE_CATEGORIES } from "@/lib/knowledge-constants";
import { reviewKnowledgeDocAction } from "@/app/(protected)/knowledge/actions";

export function DocReviewControl({
  docId,
  category,
  approved,
}: {
  docId: string;
  category: string;
  approved: boolean;
}) {
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  function save(change: { category?: string; approved?: boolean }) {
    setError(null);
    startTransition(async () => {
      const result = await reviewKnowledgeDocAction(docId, change);
      if (result.status === "error") setError(result.message);
    });
  }

  return (
    <div className="flex flex-col items-start gap-1">
      <div className="flex flex-wrap items-center gap-3 text-[12px]">
        <label className="flex items-center gap-1.5 text-muted-foreground">
          Category
          <select
            value={category}
            disabled={isPending}
            onChange={(e) => save({ category: e.target.value })}
            className="rounded-md border border-border bg-background px-2 py-1 text-[12px] text-foreground"
          >
            {KNOWLEDGE_CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <span className={approved ? "text-foreground" : "font-semibold text-[var(--danger-fg)]"}>
          {approved ? "Used by the agent" : "Not used by the agent"}
        </span>
        <button
          type="button"
          disabled={isPending}
          onClick={() => save({ approved: !approved })}
          className="text-[12px] font-semibold text-foreground underline underline-offset-2 hover:no-underline"
        >
          {approved ? "Stop using" : "Use it"}
        </button>
      </div>
      {error ? (
        <p role="alert" className="text-[11.5px] text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}
