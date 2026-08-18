/**
 * Calendly hosted-handoff flow (SR-6 decisions 1/8, scope items 12/13).
 *
 * Rendered by `Bubble.tsx` for a bot message carrying
 * `action="calendly_handoff"`. Two steps, both required, in order:
 *   1. A still-required "where should we send the invite?" email step
 *      (decision 8 -- the double-ask is accepted; it exists ONLY to write
 *      the server-side correlation intent that lets the later webhook
 *      backfill visitor_id onto the Calendly booking), plus name (required)
 *      and phone (optional) so this visitor becomes a real Lead the admin
 *      console can see -- scheduling's own `calendly_handoff_intents` row
 *      is a short-lived, email-only correlation record, never visible in
 *      any admin UI, so capturing a Lead is a SEPARATE write via the same
 *      `submitLead` call `LeadForm` uses (lead-capture-crm owns Lead
 *      records; scheduling never creates one itself). Submitting fires
 *      both calls concurrently: `postHandoffIntent` is still the
 *      LOAD-BEARING one -- the link-out button is NEVER revealed before
 *      its `ok: true` response, never open the link without recording the
 *      intent. `submitLead` is best-effort alongside it (mirrors the
 *      Calendly webhook's own best-effort reminder/confirmation writes):
 *      its failure is logged (PII-safe) but never blocks the booking
 *      hand-off, since a secondary CRM write must not break the primary
 *      "get the visitor to Calendly" flow.
 *   2. A link-out button that does exactly
 *      `window.open(schedulingUrl, "_blank", "noopener,noreferrer")`
 *      (decision 1, LOAD-BEARING) -- never an injected Calendly
 *      `<script>`/iframe, so the widget stays self-contained/zero-third-
 *      party on any host page regardless of that host's CSP.
 *
 * Honest error + manual retry on a `postHandoffIntent` failure -- never a
 * fabricated "recorded" state. PII-safe console logging (error_code/status/
 * correlation_id only, never the typed email/name/phone).
 */
import { useState } from "react";

import type { WidgetConfig } from "../config";
import { submitLead } from "../lead";
import { formatUsPhoneInput } from "../phoneFormat";
import { SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT, postHandoffIntent, type AvailabilitySummary } from "../schedule";

const LOG_PREFIX = "[chatbot-widget]";

export interface CalendlyHandoffProps {
  config: WidgetConfig;
  summary: AvailabilitySummary;
}

type Step =
  | { name: "email" }
  | { name: "submitting" }
  | { name: "ready" }
  | { name: "error"; message: string };

export function CalendlyHandoff({ config, summary }: CalendlyHandoffProps) {
  const [step, setStep] = useState<Step>({ name: "email" });
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const schedulingUrl = summary.schedulingUrl;

  async function submitDetails() {
    if (!email.trim() || !name.trim()) return;
    setStep({ name: "submitting" });

    const consent = { granted: true as const, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT };
    const [handoffResult, leadResult] = await Promise.all([
      postHandoffIntent(config, { email: email.trim() }),
      submitLead(config, {
        name: name.trim(),
        email: email.trim(),
        ...(phone.trim() ? { phone: phone.trim() } : {}),
        consent,
      }),
    ]);

    if (!leadResult.ok) {
      // Best-effort (mirrors the Calendly webhook's own best-effort
      // reminder/confirmation writes): a failed CRM write must never block
      // the primary "get the visitor to Calendly" flow below.
      const { errorCode, correlationId, status } = leadResult.error;
      console.error(
        `${LOG_PREFIX} submitLead failed: ${errorCode} (status=${status ?? "n/a"}, correlation_id=${correlationId ?? "n/a"})`,
      );
    }

    if (!handoffResult.ok) {
      const { errorCode, correlationId, status } = handoffResult.error;
      console.error(
        `${LOG_PREFIX} postHandoffIntent failed: ${errorCode} (status=${status ?? "n/a"}, correlation_id=${correlationId ?? "n/a"})`,
      );
      setStep({ name: "error", message: "Sorry — we couldn't record your details. Please try again." });
      return;
    }
    setStep({ name: "ready" });
  }

  function openScheduling() {
    if (!schedulingUrl) return;
    // Decision 1 (LOAD-BEARING): a link-out only, never an injected
    // Calendly script/iframe. noopener,noreferrer -- no window.opener
    // leakage to the third-party tab.
    window.open(schedulingUrl, "_blank", "noopener,noreferrer");
  }

  if (!schedulingUrl) {
    // Defensive -- the server never sends calendly_handoff without a URL,
    // but never render a dead-end link-out button either (honest failure).
    return (
      <div className="cw-sched-handoff cw-sched-error" role="alert">
        Sorry — scheduling isn&rsquo;t available right now. Please try again later.
      </div>
    );
  }

  if (step.name === "ready") {
    return (
      <div className="cw-sched-handoff" role="status">
        <p>Thanks! Click below to pick a time on our calendar.</p>
        <button
          type="button"
          className="cw-sched-handoff-link-button"
          onClick={openScheduling}
          aria-label="Open our scheduling page (opens in a new tab)"
        >
          Open scheduling page
        </button>
      </div>
    );
  }

  const submitting = step.name === "submitting";

  return (
    <div className="cw-sched-handoff">
      <label className="cw-sched-email-label" htmlFor="cw-sched-handoff-email">
        Where should we send the invite?
      </label>
      <input
        id="cw-sched-handoff-email"
        className="cw-lead-input"
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        disabled={submitting}
        required
        placeholder="you@example.com"
        autoComplete="email"
        aria-label="Invite email"
      />

      <label className="cw-sched-email-label" htmlFor="cw-sched-handoff-name">
        Your name
      </label>
      <input
        id="cw-sched-handoff-name"
        className="cw-lead-input"
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        disabled={submitting}
        required
        placeholder="Jane Doe"
        autoComplete="name"
        aria-label="Your name"
      />

      <label className="cw-sched-email-label" htmlFor="cw-sched-handoff-phone">
        Phone number (optional)
      </label>
      <input
        id="cw-sched-handoff-phone"
        className="cw-lead-input"
        type="tel"
        inputMode="numeric"
        value={phone}
        onChange={(e) => setPhone(formatUsPhoneInput(e.target.value))}
        disabled={submitting}
        placeholder="(555) 123-4567"
        autoComplete="tel"
        aria-label="Phone number, optional"
      />

      <p className="cw-sched-handoff-consent-note">{SCHEDULE_CONSENT_TEXT}</p>

      {step.name === "error" && (
        <div className="cw-sched-error" role="alert">
          {step.message}
        </div>
      )}

      <button
        type="button"
        className="cw-sched-handoff-continue-button"
        disabled={!email.trim() || !name.trim() || submitting}
        onClick={() => void submitDetails()}
      >
        {submitting ? "Continuing…" : "Continue"}
      </button>
    </div>
  );
}
