"use client";

/**
 * "Test the bot" -- Train the Agent's stateless preview chat (replaces the
 * former `TestBotCard` placeholder). Calls `previewChat` (POST
 * /admin/training/chat) per message -- a real RAG/orchestrator turn, but
 * NOTHING is persisted server-side (no conversation, no message rows): a
 * fresh page load starts a fresh, empty chat, honestly reflecting that
 * nothing here is saved. This is single-turn testing, not a conversation
 * simulator -- each question is answered independently, with no memory of
 * earlier questions in this same session.
 *
 * When a reply's decision isn't "answer" (clarify/escalate/blocked), an
 * inline "Teach the correct answer" form appears under that specific
 * reply -- submitting it calls `submitTrainedAnswer`, which pushes the Q&A
 * through the real ingestion pipeline so a similar future question (here or
 * on the live widget) gets a real answer instead of an escalation.
 *
 * That form's "Suggest a reply" button calls `suggestDraftAnswer`
 * (`suggest_draft_answer` -- same LLM/RAG stack as the chat above, but
 * bypasses the confidence gate to always draft something) and fills the
 * textarea with the result for the admin to review/edit -- it is never
 * auto-saved; "Save answer" still requires an explicit click.
 */
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  previewChat,
  submitTrainedAnswer,
  suggestDraftAnswer,
  type PreviewChatResult,
} from "@/app/(protected)/knowledge/actions";
import { cn } from "@/lib/utils";

interface ChatTurn {
  id: string;
  question: string;
  result: PreviewChatResult;
  taught: boolean;
}

const DECISION_LABEL: Record<string, string> = {
  answer: "Answered",
  clarify: "Asked to clarify",
  escalate: "Couldn't answer",
  blocked: "Blocked by guardrail",
};

export function TeachForm({
  question,
  tenantId,
  onTaught,
}: {
  question: string;
  tenantId?: string;
  onTaught: () => void;
}) {
  const inputId = useId();
  const [answer, setAnswer] = useState("");
  const [saving, setSaving] = useState(false);
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
    const result = await submitTrainedAnswer(question, answer.trim());
    setSaving(false);
    if (result.status === "error") {
      setError(result.message);
      return;
    }
    onTaught();
  }

  return (
    <div className="mt-2 flex flex-col gap-2 rounded-[10px] border border-[var(--line)] bg-background p-3">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={inputId} className="text-[11.5px] font-semibold text-foreground">
          Teach the correct answer
        </label>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => void handleSuggest()}
          disabled={suggesting || saving}
          className="h-auto px-2 py-1 text-[10.5px]"
        >
          {suggesting ? "Drafting…" : "Suggest a reply"}
        </Button>
      </div>
      <Textarea
        id={inputId}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        placeholder="What should the bot say to this question?"
        className="min-h-[70px] text-[12.5px]"
        disabled={saving}
      />
      {error ? <p className="text-[11.5px] text-[var(--danger-fg)]">{error}</p> : null}
      <Button
        type="button"
        size="sm"
        onClick={() => void handleSave()}
        disabled={saving || !answer.trim()}
        className="self-start"
      >
        {saving ? "Saving…" : "Save answer"}
      </Button>
    </div>
  );
}

export function TestBotChat({ tenantId }: { tenantId?: string }) {
  const inputId = useId();
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sending, setSending] = useState(false);

  async function handleSend() {
    const question = message.trim();
    if (!question) return;
    setMessage("");
    setSending(true);
    const result = await previewChat(question, tenantId);
    setSending(false);
    setTurns((prev) => [...prev, { id: crypto.randomUUID(), question, result, taught: false }]);
  }

  function markTaught(id: string) {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, taught: true } : t)));
  }

  return (
    <div className="flex flex-col gap-2 rounded-[14px] border border-[var(--line)] px-[22px] py-5">
      <p className="text-[15px] font-semibold text-foreground">Test the bot</p>
      <p className="text-[11.5px] text-muted-foreground">
        Ask a question the way a visitor would. Nothing here is saved to conversation history --
        each question is tested independently.
      </p>

      {turns.length > 0 ? (
        <ul className="mt-1 flex flex-col gap-3">
          {turns.map((turn) => (
            <li key={turn.id} className="flex flex-col gap-1.5">
              <p className="rounded-[9px] bg-secondary px-3 py-2 text-[12.5px] text-foreground">
                {turn.question}
              </p>
              {turn.result.status === "error" ? (
                <p className="rounded-[9px] border border-[var(--danger-border)] bg-[#f6e3df] px-3 py-2 text-[12px] text-[var(--danger-fg)]">
                  {turn.result.message}
                </p>
              ) : (
                <div className="rounded-[9px] border border-[var(--line)] px-3 py-2">
                  <p className="text-[12.5px] text-foreground">{turn.result.reply}</p>
                  <p className="mt-1 text-[10.5px] font-semibold uppercase text-muted-foreground">
                    {DECISION_LABEL[turn.result.decision] ?? turn.result.decision}
                    {turn.result.confidence !== null
                      ? ` · ${Math.round(turn.result.confidence * 100)}% confidence`
                      : ""}
                  </p>
                  {turn.result.decision !== "answer" ? (
                    turn.taught ? (
                      <p className="mt-2 text-[11.5px] font-medium text-[#3f7d57]">
                        Saved — ask again in a few seconds to see it take effect.
                      </p>
                    ) : (
                      <TeachForm
                        question={turn.question}
                        tenantId={tenantId}
                        onTaught={() => markTaught(turn.id)}
                      />
                    )
                  ) : null}
                </div>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      <label htmlFor={inputId} className="sr-only">
        Test question
      </label>
      <input
        id={inputId}
        type="text"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void handleSend();
          }
        }}
        placeholder="Ask a question to preview the answer…"
        disabled={sending}
        className="mt-1 h-[42px] rounded-[10px] border border-[var(--line)] px-3 text-[12px] text-foreground disabled:cursor-not-allowed disabled:bg-background"
      />
      <Button
        type="button"
        size="sm"
        onClick={() => void handleSend()}
        disabled={sending || !message.trim()}
        className={cn("mt-1 w-fit self-start")}
      >
        {sending ? "Asking…" : "Run test"}
      </Button>
    </div>
  );
}
