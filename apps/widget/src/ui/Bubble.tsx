/**
 * Chat bubble rendering (S14.2 scope item 3, S14.3 decision 1, S14.4
 * decision 1): user/bot/system bubbles, plus the typing/thinking indicator
 * as a distinct list item. A bot message carrying `action="lead_form"`
 * renders the real consent-gated `<LeadForm>` (S14.3); `action="schedule_cta"`
 * renders the real consent-gated `<ScheduleCta>` (S14.4). `ActionStub` has
 * no live callers after S14.4 and has been removed.
 *
 * SR-5 decision 5: when the persistent "Connect with a sales rep" CTA
 * (not an orchestrator turn) triggers the flow, the bot bubble carries the
 * server-authoritative `scheduleSummary` alongside `action`, so the SAME
 * in-thread `<ScheduleCta>` renders the staged calendar/grid/timezone/
 * email picker (decision 1) rather than a separate panel element outside
 * the message list.
 *
 * SR-14 D4/D8: `action="identity_form"` renders `<IdentityForm>` -- the
 * conversation-start identity gate's inline capture form, a SIBLING of
 * `<LeadForm>` (distinct consent purpose/copy, distinct endpoint), never a
 * replacement. `onIdentityCaptured` is threaded through from `ChatWidget` so
 * capture success can trigger D3's automatic re-send of the visitor's
 * deferred original question -- `Bubble`/`IdentityForm` have no opinion
 * about what happens after capture, they only report it.
 */
import { Markdown } from "./Markdown";
import { LeadForm } from "./LeadForm";
import { IdentityForm } from "./IdentityForm";
import { ScheduleCta } from "./ScheduleCta";
import { CalendlyHandoff } from "./CalendlyHandoff";
import type { WidgetConfig } from "../config";
import type { AvailabilitySummary } from "../schedule";

export interface ChatMessage {
  id: string;
  role: "user" | "bot" | "system-error";
  text: string;
  /** Only ever set on a bot message (decision 5). SR-6 adds
   * "calendly_handoff" -- a Calendly-configured tenant's hosted-handoff
   * flow, rendered by <CalendlyHandoff> instead of the native <ScheduleCta>
   * picker (SR-6 decision 1). SR-14 adds "identity_form" -- the
   * conversation-start identity gate's inline capture form. */
  action?: "lead_form" | "schedule_cta" | "calendly_handoff" | "identity_form" | null;
  /** Only ever set on a bot message carrying action="schedule_cta"/
   * "calendly_handoff" that originated from the persistent CTA (SR-5
   * decision 5), never from an orchestrator turn. */
  scheduleSummary?: AvailabilitySummary;
}

export function Bubble({
  message,
  config,
  onIdentityCaptured,
}: {
  message: ChatMessage;
  config: WidgetConfig;
  /** SR-14 D3: called on a successful identity capture so the caller can
   * auto-re-send the visitor's deferred original question. */
  onIdentityCaptured?: () => void;
}) {
  if (message.role === "system-error") {
    return (
      <div className="cw-line cw-line-error" role="alert">
        {message.text}
      </div>
    );
  }

  const isUser = message.role === "user";
  return (
    <div className={`cw-bubble-row ${isUser ? "cw-bubble-row-user" : "cw-bubble-row-bot"}`}>
      <div className={`cw-bubble ${isUser ? "cw-bubble-user" : "cw-bubble-bot"}`}>
        {isUser ? message.text : <Markdown text={message.text} />}
        {!isUser && message.action === "lead_form" ? <LeadForm config={config} /> : null}
        {!isUser && message.action === "identity_form" ? (
          <IdentityForm config={config} {...(onIdentityCaptured ? { onCaptured: onIdentityCaptured } : {})} />
        ) : null}
        {!isUser && message.action === "schedule_cta" ? (
          <ScheduleCta config={config} {...(message.scheduleSummary ? { summary: message.scheduleSummary } : {})} />
        ) : null}
        {!isUser && message.action === "calendly_handoff" && message.scheduleSummary ? (
          <CalendlyHandoff config={config} summary={message.scheduleSummary} />
        ) : null}
      </div>
    </div>
  );
}

/** The typing/thinking indicator, shown as a distinct list item while a turn is in flight. */
export function TypingIndicator() {
  return (
    <div className="cw-bubble-row cw-bubble-row-bot" aria-live="off">
      <div className="cw-bubble cw-bubble-bot cw-typing" role="status" aria-label="Bot is typing">
        <span className="cw-typing-dot" />
        <span className="cw-typing-dot" />
        <span className="cw-typing-dot" />
      </div>
    </div>
  );
}
