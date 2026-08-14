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
  onSuggestion: (message: string) => void;
  /** SR-14 D3: threaded through to `<Bubble>`/`<IdentityForm>` so a
   * successful identity capture can trigger the deferred-question re-send. */
  onIdentityCaptured?: () => void;
  onHandoffTalk?: () => void;
  onHandoffStay?: () => void;
}

/** The "Book a call with sales" chip's message/label (exported so
 * `ChatWidget.tsx#handleSuggestion` can route a click on this specific chip
 * through `startScheduling` -- the same explicit-booking path as the
 * persistent "Connect with a sales rep" button -- instead of `sendMessage`/
 * the orchestrator turn, without duplicating the literal string). */
export const BOOK_CALL_SUGGESTION_MESSAGE = "Book a call with sales";

// FCRS deployment: replaced the generic SaaS placeholder chips ("How does
// the Pro plan compare?" / "I need help with a problem") with roofing/
// solar-relevant equivalents -- same icon/intent pairing (an informational
// "chat" question, an unchanged "calendar" booking chip, and an urgent
// "support" question), just on-topic for this business.
const SUGGESTIONS = [
  { message: "Do you offer free roof inspections?", label: "Do you offer free roof inspections?", icon: "chat" },
  { message: BOOK_CALL_SUGGESTION_MESSAGE, label: BOOK_CALL_SUGGESTION_MESSAGE, icon: "calendar" },
  { message: "I have a roof leak, can you help?", label: "I have a roof leak, can you help?", icon: "support" },
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
  if (name === "support") {
    return <svg aria-hidden="true" {...common}><path d="M4.9 4.9a10 10 0 0 1 14.2 0M4.9 19.1a10 10 0 0 0 14.2 0M2 12h4m12 0h4" /><circle cx="12" cy="12" r="4" /></svg>;
  }
  return <svg aria-hidden="true" {...common}><path d="M21 11.5a8.4 8.4 0 0 1-9 8.5 9.7 9.7 0 0 1-4.1-.9L3 21l1.9-4.1A8.4 8.4 0 0 1 3 11.5a8.5 8.5 0 0 1 18 0Z" /></svg>;
}

export function MessageList({ messages, pending, config, onSuggestion, onIdentityCaptured, onHandoffTalk, onHandoffStay }: MessageListProps) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const selectedSuggestion = [...messages].reverse().find(
    (message) => message.role === "user" && SUGGESTIONS.some((suggestion) => suggestion.message === message.text),
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
          <h2 id="cw-welcome-heading">Hi, I&rsquo;m Rebecca</h2>
          <p>I can help with support, sales and product questions. What would you like to do?</p>
          <div className="cw-suggestions" aria-label="Suggested questions">
            {SUGGESTIONS.map((suggestion) => (
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
        />
      ))}
      {pending && <TypingIndicator />}
      <div ref={endRef} />
    </div>
  );
}
