"use client";

/**
 * "Coverage check" -- Train the Agent's list of real visitor questions the
 * bot didn't answer (replaces the former `CoverageCheckCard` placeholder).
 * Fed the server-fetched `ListGapsResult` (mirrors `doc-list.tsx`'s
 * server-fetch-then-client-render split), each row has an inline "Teach the
 * correct answer" form AND a "Not a real question" dismiss action -- some
 * gaps are junk/adversarial visitor messages (e.g. "I won't.") rather than
 * a real answerable question, and forcing an admin to invent a fake answer
 * just to clear those from the queue would be worse than leaving them. Both
 * paths revalidate `/knowledge` server-side, so the row disappears from
 * THIS list on next load (client-side, it just hides itself immediately for
 * responsive feedback -- `revalidatePath` will confirm it on the next
 * navigation regardless).
 *
 * Honest empty state, no fabricated rows -- same no-invented-data
 * convention as `doc-list.tsx`.
 *
 * Each row's "Suggest a reply" button calls `suggestDraftAnswer`
 * (`suggest_draft_answer` -- bypasses the confidence gate to always draft
 * something) and fills the textarea with the result for the admin to
 * review/edit -- never auto-saved, mirrors `test-bot-chat.tsx`'s identical
 * teach-form affordance.
 */
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  dismissGap,
  submitTrainedAnswer,
  suggestDraftAnswer,
  type ListGapsResult,
} from "@/app/(protected)/knowledge/actions";

function GapRow({
  question,
  questionMessageId,
  decision,
  confidence,
  tenantId,
  onHandled,
}: {
  question: string;
  questionMessageId: string;
  decision: string;
  confidence: number | null;
  tenantId?: string;
  onHandled: () => void;
}) {
  const inputId = useId();
  const [answer, setAnswer] = useState("");
  const [saving, setSaving] = useState(false);
  const [dismissing, setDismissing] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSuggest() {
    setSuggesting(true);
    setError(null);
    const result = await suggestDraftAnswer(question, tenantId);
    setSuggesting(false);
    if (result.status === "error") {
      setError(result.message);
      return;
    }
    setAnswer(result.suggestion);
  }

  async function handleSave() {
    if (!answer.trim()) return;
    setSaving(true);
    setError(null);
    const result = await submitTrainedAnswer(question, answer.trim(), questionMessageId, tenantId);
    setSaving(false);
    if (result.status === "error") {
      setError(result.message);
      return;
    }
    onHandled();
  }

  async function handleDismiss() {
    setDismissing(true);
    setError(null);
    const result = await dismissGap(question, questionMessageId, tenantId);
    setDismissing(false);
    if (result.status === "error") {
      setError(result.message);
      return;
    }
    onHandled();
  }

  const busy = saving || dismissing || suggesting;

  return (
    <li className="rounded-[10px] border border-[var(--line)] bg-background p-3.5">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] font-medium text-foreground">{question}</p>
        <span className="shrink-0 rounded-full bg-secondary px-2 py-0.5 text-[10.5px] font-semibold uppercase text-muted-foreground">
          {decision}
          {confidence !== null ? ` · ${Math.round(confidence * 100)}%` : ""}
        </span>
      </div>
      <div className="mt-2 flex flex-col gap-2">
        <div className="flex justify-end">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void handleSuggest()}
            disabled={busy}
            className="h-auto px-2 py-1 text-[10.5px]"
          >
            {suggesting ? "Drafting…" : "Suggest a reply"}
          </Button>
        </div>
        <label htmlFor={inputId} className="sr-only">
          Correct answer
        </label>
        <Textarea
          id={inputId}
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          placeholder="What should the bot say to this question?"
          className="min-h-[60px] text-[12.5px]"
          disabled={busy}
        />
        {error ? <p className="text-[11.5px] text-[var(--danger-fg)]">{error}</p> : null}
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            onClick={() => void handleSave()}
            disabled={busy || !answer.trim()}
          >
            {saving ? "Saving…" : "Save answer"}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => void handleDismiss()}
            disabled={busy}
          >
            {dismissing ? "Dismissing…" : "Not a real question"}
          </Button>
        </div>
      </div>
    </li>
  );
}

export function CoverageGaps({ result, tenantId }: { result: ListGapsResult; tenantId?: string }) {
  const [hiddenIds, setHiddenIds] = useState<Set<string>>(new Set());

  if (result.status === "error") {
    return (
      <div
        role="alert"
        className="rounded-[10px] bg-primary px-3.5 py-3.5 text-[12px] text-primary-foreground"
      >
        Unable to load coverage gaps. {result.message}
      </div>
    );
  }

  const visible = result.gaps.filter((g) => !hiddenIds.has(g.messageId));

  return (
    <div className="flex flex-col gap-2 rounded-[14px] bg-primary px-[22px] py-5">
      <p className="text-[15px] font-semibold text-primary-foreground">Coverage check</p>
      <p className="text-[12.5px] leading-relaxed text-[#c9c9c9]">
        Questions your bot couldn&apos;t answer for real visitors -- teach an answer to fix it, or
        dismiss it if it isn&apos;t a real question.
      </p>
      {visible.length === 0 ? (
        <p
          role="status"
          className="mt-1 rounded-[10px] bg-white/[.08] px-3.5 py-3.5 text-[12px] text-primary-foreground/80"
        >
          No gaps right now — visitors&apos; questions are all being answered.
        </p>
      ) : (
        <ul className="mt-1 flex flex-col gap-2.5">
          {visible.map((gap) => (
            <GapRow
              key={gap.messageId}
              question={gap.question}
              questionMessageId={gap.questionMessageId}
              decision={gap.decision}
              confidence={gap.confidence}
              tenantId={tenantId}
              onHandled={() => setHiddenIds((prev) => new Set(prev).add(gap.messageId))}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
