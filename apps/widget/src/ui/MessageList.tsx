/**
 * Scrollable message list (S14.2 scope item 3). Auto-scrolls to the newest
 * message/indicator whenever the list changes.
 *
 * S14.5 decision 2: an explicit live-region politeness policy —
 * `aria-live="polite"` + `aria-relevant="additions"` so appended bot/error
 * lines are announced without interrupting; the user's own sent message is
 * not specially suppressed (a single polite echo is acceptable per the
 * spec's Tests wording) but the typing indicator (`Bubble.tsx`'s
 * `TypingIndicator`) stays `aria-live="off"` and is unaffected by this
 * container-level policy since screen readers honor the nearest live
 * region on the changed subtree.
 */
import { useEffect, useRef } from "react";

import { Bubble, TypingIndicator, type ChatMessage } from "./Bubble";
import type { WidgetConfig } from "../config";

export interface MessageListProps {
  messages: ChatMessage[];
  pending: boolean;
  config: WidgetConfig;
  botName: string;
  /** Tenant-configured suggestion chips (widget branding/personalization
   * decision 2/3). When absent/empty, falls back to the hardcoded FCRS-demo
   * `SUGGESTIONS` below (unchanged default behavior, sentinel-routing
   * included). Custom suggestions are always sent as plain messages -- see
   * decision 3's rationale for not extending the booking sentinel to them. */
  suggestions?: string[];
  onSuggestion: (message: string) => void;
  /** SR-14 D3: threaded through to `<Bubble>`/`<IdentityForm>` so a
   * successful identity capture can trigger the deferred-question re-send. */
  onIdentityCaptured?: () => void;
  onHandoffTalk?: () => void;
  onHandoffStay?: () => void;
  /** Threaded through to `<Bubble>`/`<ScheduleCta>` -- see Bubble.tsx's own doc. */
  onBooked?: () => void;
}

/** The "Book a call with sales" chip's message/label (exported so
 * `ChatWidget.tsx#handleSuggestion` can route a click on this specific chip
 * through `startScheduling` -- the same explicit-booking path as the
 * persistent "Connect with a sales rep" button -- instead of `sendMessage`/
 * the orchestrator turn, without duplicating the literal string). */
export const BOOK_CALL_SUGGESTION_MESSAGE = "Book a call with sales";

// FCRS deployment: chips pitch the marketing-chatbot product itself to a
// prospective roofing/solar company owner evaluating this demo (not a
// homeowner's roofing questions) -- lead-gen/growth pain points plus a
// direct booking chip. The booking chip's message MUST stay exactly
// BOOK_CALL_SUGGESTION_MESSAGE (label may read differently) since
// ChatWidget.tsx#handleSuggestion matches on it by strict equality to route
// straight to scheduling, bypassing the orchestrator turn.
//
// The roof-inspections chip is rephrased as a question, unlike the other
// "I want..." chips. Live traffic (checked directly against stored
// conversation intent/decision columns) showed the orchestrator's LLM intent
// classifier reads ANY declarative "I want more roof inspections[...]"
// phrasing as intent="scheduling_request" -- not just the word "booked" (a
// first fix that only dropped "booked" was confirmed, via the same live
// data, to still misclassify). "scheduling_request" skips RAG/knowledge-base
// retrieval entirely and short-circuits straight to the fixed escalate reply
// (see api.orchestrator.service's intent branch) -- no uploaded
// knowledge-base content could ever answer it, regardless of what's there.
// "How do you get me more roof inspections booked?" -- the visitor asking
// about the AGENCY's process, not stating a personal action-request --
// avoids the misclassification (confirmed live: this exact text returns a
// real knowledge-base answer, not the escalate template) and matches the
// uploaded knowledge-base entry's own Var 1 phrasing verbatim. label and
// message are kept IDENTICAL here (unlike BOOK_CALL_SUGGESTION_MESSAGE
// above) so the chip never shows different text than what appears once
// clicked.
const SUGGESTIONS = [
  { message: "I want more qualified roofing leads", label: "I want more qualified roofing leads", icon: "chat" },
  {
    message: "How do you get me more roof inspections booked?",
    label: "How do you get me more roof inspections booked?",
    icon: "chat",
  },
  { message: "I want to generate leads with Google Ads", label: "I want to generate leads with Google Ads", icon: "chat" },
  { message: "I want to generate leads with Meta Ads", label: "I want to generate leads with Meta Ads", icon: "chat" },
  { message: BOOK_CALL_SUGGESTION_MESSAGE, label: "Book a call with Sales", icon: "calendar" },
] as const;

function SuggestionGlyph({ name }: { name: (typeof SUGGESTIONS)[number]["icon"] }) {
  const common = {
    width: 18,
    height: 18,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.9,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };

  if (name === "calendar") {
    return <svg aria-hidden="true" {...common}><path d="M6 2v4M18 2v4M3 9h18" /><rect x="3" y="4" width="18" height="17" rx="3" /></svg>;
  }
  return <svg aria-hidden="true" {...common}><path d="M21 11.5a8.4 8.4 0 0 1-9 8.5 9.7 9.7 0 0 1-4.1-.9L3 21l1.9-4.1A8.4 8.4 0 0 1 3 11.5a8.5 8.5 0 0 1 18 0Z" /></svg>;
}

export function MessageList({ messages, pending, config, botName, suggestions, onSuggestion, onIdentityCaptured, onHandoffTalk, onHandoffStay, onBooked }: MessageListProps) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const effectiveSuggestions =
    suggestions && suggestions.length > 0
      ? suggestions.map((text) => ({ message: text, label: text, icon: "chat" as const }))
      : SUGGESTIONS;
  const selectedSuggestion = [...messages].reverse().find(
    (message) => message.role === "user" && effectiveSuggestions.some((suggestion) => suggestion.message === message.text),
  )?.text;

  useEffect(() => {
    // jsdom (this repo's Vitest environment) does not implement
    // scrollIntoView — guard defensively so tests don't need to polyfill it
    // and a real browser without it (unlikely, but cheap to guard) doesn't
    // throw into the host page.
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, pending]);

  return (
    <div className="cw-message-list" role="log" aria-live="polite" aria-relevant="additions">
      <section className="cw-welcome" aria-labelledby="cw-welcome-heading">
          <span className="cw-welcome-orb" aria-hidden="true" />
          <h2 id="cw-welcome-heading">Hi, I&rsquo;m {botName}</h2>
          <p>I can help with support, sales and product questions. What would you like to do?</p>
          <div className="cw-suggestions" aria-label="Suggested questions">
            {effectiveSuggestions.map((suggestion) => (
              <button
                key={suggestion.message}
                type="button"
                className={`cw-suggestion${selectedSuggestion === suggestion.message ? " cw-suggestion-selected" : ""}`}
                aria-pressed={selectedSuggestion === suggestion.message}
                onClick={() => onSuggestion(suggestion.message)}
              >
                <span className="cw-suggestion-icon"><SuggestionGlyph name={suggestion.icon} /></span>
                <span>{suggestion.label}</span>
              </button>
            ))}
          </div>
      </section>
      {messages.map((message) => (
        <Bubble
          key={message.id}
          message={message}
          config={config}
          {...(onIdentityCaptured ? { onIdentityCaptured } : {})}
          {...(onHandoffTalk ? { onHandoffTalk } : {})}
          {...(onHandoffStay ? { onHandoffStay } : {})}
          {...(onBooked ? { onBooked } : {})}
        />
      ))}
      {pending && <TypingIndicator />}
      <div ref={endRef} />
    </div>
  );
}
