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
}

const SUGGESTIONS = ["What does your product do?", "How much does it cost?", "Book a call with sales"];

export function MessageList({ messages, pending, config, onSuggestion, onIdentityCaptured }: MessageListProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // jsdom (this repo's Vitest environment) does not implement
    // scrollIntoView — guard defensively so tests don't need to polyfill it
    // and a real browser without it (unlikely, but cheap to guard) doesn't
    // throw into the host page.
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, pending]);

  return (
    <div className="cw-message-list" role="log" aria-live="polite" aria-relevant="additions">
      {messages.length === 0 && !pending && (
        <section className="cw-welcome" aria-labelledby="cw-welcome-heading">
          <span className="cw-welcome-orb" aria-hidden="true" />
          <h2 id="cw-welcome-heading">Hi, I&rsquo;m your assistant</h2>
          <p>Ask about the product, pricing, or arrange a call with the team.</p>
          <div className="cw-suggestions" aria-label="Suggested questions">
            {SUGGESTIONS.map((suggestion) => (
              <button key={suggestion} type="button" className="cw-suggestion" onClick={() => onSuggestion(suggestion)}>
                <span>{suggestion}</span>
              </button>
            ))}
          </div>
        </section>
      )}
      {messages.map((message) => (
        <Bubble
          key={message.id}
          message={message}
          config={config}
          {...(onIdentityCaptured ? { onIdentityCaptured } : {})}
        />
      ))}
      {pending && <TypingIndicator />}
      <div ref={endRef} />
    </div>
  );
}
