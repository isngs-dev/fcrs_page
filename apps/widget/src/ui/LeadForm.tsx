/**
 * Consent-gated inline lead capture form (S14.3 decisions 2/5/6, scope item 2).
 *
 * Replaces the `lead_form` action stub with a real form: name (required),
 * email (required), phone (optional), and an unchecked-by-default consent
 * checkbox. Submit is disabled until name + email are non-blank AND consent
 * is checked — this makes the backend's `422 CONSENT_REQUIRED` unreachable
 * by construction. On a real `201`, the form is replaced by an honest,
 * non-resubmittable confirmation. On any failure, an honest error line
 * appears, the form re-enables, and no success is ever fabricated. All
 * state is in-memory only (component state) — nothing is persisted, and PII
 * is never logged (failure console.error carries only error_code/
 * correlation_id/status).
 *
 * S14.5 a11y hardening only (no behavior/consent/request change): focus
 * moves to the success confirmation when it appears (it was previously
 * reachable only visually), matching the `role="status"`/`role="alert"`
 * announcement semantics that already existed here.
 */
import { useEffect, useRef, useState } from "react";

import type { WidgetConfig } from "../config";
import { CONSENT_PURPOSE, CONSENT_TEXT, submitLead } from "../lead";
import { formatUsPhoneInput } from "../phoneFormat";

const LOG_PREFIX = "[chatbot-widget]";

export interface LeadFormProps {
  config: WidgetConfig;
}

export function LeadForm({ config }: LeadFormProps) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [consentChecked, setConsentChecked] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [succeeded, setSucceeded] = useState(false);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const confirmationRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // Focus the first field when the form appears (baseline semantics —
    // the full a11y/focus-management audit is S14.5).
    nameInputRef.current?.focus();
  }, []);

  useEffect(() => {
    // S14.5: focus the success confirmation when it appears, so screen
    // reader / keyboard users land on it rather than it only being
    // announced via role="status" while focus stays on the (now-removed)
    // submit button.
    if (succeeded) {
      confirmationRef.current?.focus();
    }
  }, [succeeded]);

  const canSubmit = name.trim().length > 0 && email.trim().length > 0 && consentChecked && !submitting;

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;

    setSubmitting(true);
    setErrorMessage(null);

    const result = await submitLead(config, {
      name: name.trim(),
      email: email.trim(),
      ...(phone.trim() ? { phone: phone.trim() } : {}),
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    if (!result.ok) {
      const { errorCode, correlationId, status } = result.error;
      // Loud on the developer channel, PII-safe: never the name/email/phone.
      console.error(
        `${LOG_PREFIX} lead submission failed: ${errorCode} (status=${status ?? "n/a"}, correlation_id=${correlationId ?? "n/a"})`,
      );
      setSubmitting(false);
      setErrorMessage("Sorry — we couldn't save your details. Please try again.");
      return;
    }

    setSubmitting(false);
    setSucceeded(true);
  }

  if (succeeded) {
    return (
      <div className="cw-lead-confirmation" role="status" tabIndex={-1} ref={confirmationRef}>
        Thanks — we&rsquo;ve got your details and someone will be in touch.
      </div>
    );
  }

  return (
    <form className="cw-lead-form" onSubmit={(e) => void handleSubmit(e)}>
      <div className="cw-lead-field">
        <label className="cw-lead-label" htmlFor="cw-lead-name">
          Name
        </label>
        <input
          ref={nameInputRef}
          id="cw-lead-name"
          className="cw-lead-input"
          type="text"
          value={name}
          disabled={submitting}
          required
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <div className="cw-lead-field">
        <label className="cw-lead-label" htmlFor="cw-lead-email">
          Email
        </label>
        <input
          id="cw-lead-email"
          className="cw-lead-input"
          type="email"
          value={email}
          disabled={submitting}
          required
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>

      <div className="cw-lead-field">
        <label className="cw-lead-label" htmlFor="cw-lead-phone">
          Phone (optional)
        </label>
        <input
          id="cw-lead-phone"
          className="cw-lead-input"
          type="tel"
          inputMode="numeric"
          value={phone}
          disabled={submitting}
          placeholder="(555) 123-4567"
          onChange={(e) => setPhone(formatUsPhoneInput(e.target.value))}
        />
      </div>

      <div className="cw-lead-consent-row">
        <input
          id="cw-lead-consent"
          className="cw-lead-checkbox"
          type="checkbox"
          checked={consentChecked}
          disabled={submitting}
          onChange={(e) => setConsentChecked(e.target.checked)}
        />
        <label className="cw-lead-consent-label" htmlFor="cw-lead-consent">
          {CONSENT_TEXT}
        </label>
      </div>

      {errorMessage && (
        <div className="cw-lead-error" role="alert">
          {errorMessage}
        </div>
      )}

      <button type="submit" className="cw-lead-submit" disabled={!canSubmit}>
        {submitting ? "Submitting…" : "Submit"}
      </button>
    </form>
  );
}
