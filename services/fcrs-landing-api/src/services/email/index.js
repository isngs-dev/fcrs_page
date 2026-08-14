import { createEmailJsClient } from "./emailjs-client.js";
import { buildConfirmationEmail, buildInternalNotificationEmail } from "./templates.js";

// Consumed by the /internal/lead-created webhook route. Both sends are
// attempted independently and are ALWAYS non-fatal: a failure (including
// missing config) logs a warning and resolves normally. It never throws, so
// an EmailJS outage can never turn into a webhook error state.

export function createEmailService({ env = process.env, emailClient } = {}) {
  const client = emailClient !== undefined ? emailClient : createEmailJsClient(env);
  const internalRecipient = env.INTERNAL_NOTIFICATION_EMAIL;
  const confirmationTemplateId = env.EMAILJS_TEMPLATE_ID_CONFIRMATION;
  const internalTemplateId = env.EMAILJS_TEMPLATE_ID_INTERNAL;

  async function safeSend(message, label) {
    if (!client) {
      console.warn(`Email skipped (${label}): email sending is not configured.`);
      return;
    }
    try {
      await client.send(message);
    } catch (err) {
      console.warn(`Email send failed (${label}): ${err && err.message ? err.message : "unknown error"}`);
    }
  }

  return {
    /**
     * Sends the confirmation email to the submitter and the internal lead
     * notification to INTERNAL_NOTIFICATION_EMAIL. Never throws.
     */
    async sendLeadEmails(submission, { timestamp } = { timestamp: new Date().toISOString() }) {
      if (!confirmationTemplateId) {
        console.warn("Email skipped (confirmation): EMAILJS_TEMPLATE_ID_CONFIRMATION is not configured.");
      } else {
        await safeSend(buildConfirmationEmail(submission, { templateId: confirmationTemplateId }), "confirmation");
      }

      if (!internalRecipient) {
        console.warn("Email skipped (internal-notification): INTERNAL_NOTIFICATION_EMAIL is not configured.");
      } else if (!internalTemplateId) {
        console.warn("Email skipped (internal-notification): EMAILJS_TEMPLATE_ID_INTERNAL is not configured.");
      } else {
        await safeSend(
          buildInternalNotificationEmail(submission, { to: internalRecipient, timestamp, templateId: internalTemplateId }),
          "internal-notification"
        );
      }
    },
  };
}
