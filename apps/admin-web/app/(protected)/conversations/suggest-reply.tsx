"use client";

/**
 * "Suggest a reply" -- an agent-facing drafting aid. Calls `suggestReply`
 * (bound with this conversation's `conversationId`/`tenantId` in
 * `transcript-pane.tsx`, since `lib/conversations.ts` is `import
 * "server-only"`) to draft a reply to the conversation's most recent visitor
 * message via the real RAG/orchestrator pipeline (`preview_answer` --
 * the same stateless preview Train the Agent's "test the bot" uses, pointed
 * at a real message instead of a typed test question).
 *
 * Deliberately a draft-and-copy affordance, not a send button: this console
 * has no live admin-reply-into-conversation endpoint (see the take-over
 * composer's own disabled state below), so there is nothing to send the
 * draft through. The agent copies it into whatever channel they actually
 * follow up with.
 */
import { useState } from "react";
import type { SuggestReplyResult } from "@/lib/conversations";

interface SuggestReplyProps {
  suggestReplyAction: () => Promise<SuggestReplyResult>;
}

type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "loaded"; result: SuggestReplyResult };

const DECISION_LABEL: Record<string, string> = {
  answer: "Answered",
  clarify: "Needs clarification",
  escalate: "Escalated",
  blocked: "Blocked",
};

export function SuggestReply({ suggestReplyAction }: SuggestReplyProps) {
  const [state, setState] = useState<LoadState>({ status: "idle" });
  const [copied, setCopied] = useState(false);

  async function handleSuggest() {
    setState({ status: "loading" });
    setCopied(false);
    const result = await suggestReplyAction();
    setState({ status: "loaded", result });
  }

  async function handleCopy(reply: string) {
    await navigator.clipboard.writeText(reply);
    setCopied(true);
  }

  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        onClick={handleSuggest}
        disabled={state.status === "loading"}
        className="w-fit rounded-[8px] border border-border bg-card px-3 py-1.5 text-[11px] font-semibold text-foreground disabled:cursor-not-allowed disabled:opacity-60"
      >
        {state.status === "loading" ? "Drafting…" : "Suggest a reply"}
      </button>

      {state.status === "loaded" && state.result.status === "error" ? (
        <p role="alert" className="text-[11px] text-[var(--danger-fg)]">
          {state.result.message}
        </p>
      ) : null}

      {state.status === "loaded" && state.result.status === "ok"
        ? (() => {
            const { suggestion } = state.result;
            return (
              <div className="flex max-w-[520px] flex-col gap-2 rounded-[10px] border border-border bg-[#fbfbf8] p-3 text-[12px]">
                <p className="text-foreground">{suggestion.reply}</p>
                <div className="flex items-center gap-2 text-[10.5px] text-muted-foreground">
                  <span>{DECISION_LABEL[suggestion.decision] ?? suggestion.decision}</span>
                  {suggestion.confidence !== null ? (
                    <span>· confidence {suggestion.confidence.toFixed(2)}</span>
                  ) : null}
                </div>
                <button
                  type="button"
                  onClick={() => handleCopy(suggestion.reply)}
                  className="w-fit text-[10.5px] font-semibold text-[var(--muted-foreground)] underline underline-offset-2"
                >
                  {copied ? "Copied" : "Copy draft"}
                </button>
              </div>
            );
          })()
        : null}
    </div>
  );
}
