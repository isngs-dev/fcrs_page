/**
 * Consent-gated conversation-start identity capture form (SR-14 D4/D6/D7,
 * scope item 9).
 *
 * Renders inline, in the thread, on a bot message carrying
 * `action="identity_form"` (`decision="identity_gate"`). Modeled on
 * `<LeadForm>` (`LeadForm.tsx`) but is a SIBLING, not a replacement (D4) --
 * this form only ever collects name + email + consent (no phone field, per
 * the request), submits to a DIFFERENT endpoint (`POST /public/chat/identity`
 * via `submitIdentity`), and uses a NEW, distinct consent purpose/copy (D7,
 * `identity.ts`'s `CHAT_IDENTITY_CONSENT_PURPOSE`/`_TEXT`) rather than
 * `lead.ts`'s "I agree to be contacted" wording, which would over-claim the
 * lawful basis here.
 *
 * Submit is disabled until name + email are non-blank AND consent is
 * checked -- this makes the backend's `422 CONSENT_REQUIRED` unreachable by
 * construction (mirrors `<LeadForm>`'s exact contract). On a real `201`, the
 * form is replaced by an honest, non-resubmittable confirmation. On any
 * failure, an honest error line appears, the form re-enables, and no success
 * is ever fabricated (never a fake "saved!" message, per CLAUDE.md's
 * no-silent-fallback rule). PII is never logged (failure console.error
 * carries only error_code/correlation_id/status, matching `<LeadForm>`).
 *
 * `onCaptured` is called exactly once, only on a genuine 201, so the caller
 * (`ChatWidget`) can auto-re-send the visitor's deferred original question
 * (SR-14 D3) -- this component has no opinion about what happens after
 * capture; it only reports success.
 *
 * a11y treatment mirrors `<LeadForm>` exactly: focus moves to the first
 * field on mount, focus moves to the success confirmation
 * (`role="status"`) when it appears, and the error line is `role="alert"`.
 */
import { useEffect, useRef, useState } from "react";

import type { WidgetConfig } from "../config";
import { CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT, submitIdentity } from "../identity";

const LOG_PREFIX = "[chatbot-widget]";

export interface IdentityFormProps {
  config: WidgetConfig;
  /** Called exactly once, only on a genuine 201 -- never on a fabricated
   * success (SR-14 D3's foundation for the caller's auto-re-send). */
  onCaptured?: () => void;
}

export function IdentityForm({ config, onCaptured }: IdentityFormProps) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [consentChecked, setConsentChecked] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [succeeded, setSucceeded] = useState(false);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const confirmationRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // Focus the first field when the form appears, matching <LeadForm>.
    nameInputRef.current?.focus();
  }, []);

  useEffect(() => {
    // Focus the success confirmation when it appears, matching <LeadForm>'s
    // S14.5 a11y hardening.
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

    const result = await submitIdentity(config, {
      name: name.trim(),
      email: email.trim(),
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    if (!result.ok) {
      const { errorCode, correlationId, status } = result.error;
      // Loud on the developer channel, PII-safe: never the name/email.
      console.error(
        `${LOG_PREFIX} identity submission failed: ${errorCode} (status=${status ?? "n/a"}, correlation_id=${correlationId ?? "n/a"})`,
      );
      setSubmitting(false);
      setErrorMessage("Sorry — we couldn't save your details. Please try again.");
      return;
    }

    setSubmitting(false);
    setSucceeded(true);
    onCaptured?.();
  }

  if (succeeded) {
    return (
      <div className="cw-identity-confirmation" role="status" tabIndex={-1} ref={confirmationRef}>
        Thanks — answering your question now…
      </div>
    );
  }

  return (
    <form className="cw-identity-form" onSubmit={(e) => void handleSubmit(e)}>
      <div className="cw-identity-field">
        <label className="cw-identity-label" htmlFor="cw-identity-name">
          Name
        </label>
        <input
          ref={nameInputRef}
          id="cw-identity-name"
          className="cw-identity-input"
          type="text"
          value={name}
          disabled={submitting}
          required
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <div className="cw-identity-field">
        <label className="cw-identity-label" htmlFor="cw-identity-email">
          Email
        </label>
        <input
          id="cw-identity-email"
          className="cw-identity-input"
          type="email"
          value={email}
          disabled={submitting}
          required
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>

      <div className="cw-identity-consent-row">
        <input
          id="cw-identity-consent"
          className="cw-identity-checkbox"
          type="checkbox"
          checked={consentChecked}
          disabled={submitting}
          onChange={(e) => setConsentChecked(e.target.checked)}
        />
        <label className="cw-identity-consent-label" htmlFor="cw-identity-consent">
          {CHAT_IDENTITY_CONSENT_TEXT}
        </label>
      </div>

      {errorMessage && (
        <div className="cw-identity-error" role="alert">
          {errorMessage}
        </div>
      )}

      <button type="submit" className="cw-identity-submit" disabled={!canSubmit}>
        {submitting ? "Submitting…" : "Submit"}
      </button>
    </form>
  );
}
