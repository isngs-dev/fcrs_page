// The two transactional email "templates" — really just the value params
// handed to EmailJS. Variable names here MUST match the {{placeholders}}
// used in the two templates created in the EmailJS dashboard exactly —
// EmailJS does simple string substitution, so a mismatched key renders as a
// literal blank in the sent email, not an error.
//
// The form (see schema/estimate-request.js) captures a single `name` field,
// not separate first/last names, and no street address / city — only a
// zip code. `splitName` derives a best-effort first/last for templates that
// expect them. The internal-notification template must NOT reference
// {{Property Address}} / {{City}} — those fields are never collected and
// were intentionally dropped from that template rather than added to the
// form (a locked design surface — see CLAUDE.md).

const PHONE_DISPLAY = "(989) 843-4628";
const COMPANY_NAME = "First Class Roofing & Solar";

function splitName(fullName) {
  const trimmed = String(fullName ?? "").trim();
  const spaceIndex = trimmed.indexOf(" ");
  if (spaceIndex === -1) {
    return { first: trimmed, last: "" };
  }
  return {
    first: trimmed.slice(0, spaceIndex),
    last: trimmed.slice(spaceIndex + 1).trim(),
  };
}

function formatDate(dateValue) {
  return dateValue && String(dateValue).trim() !== "" ? String(dateValue) : "not specified";
}

/**
 * Builds the EmailJS send params for the confirmation email sent to the
 * submitter. Variable names match the "FCRS Confirmation" template:
 * {{First Name}}, {{Company Name}}, {{Phone Number}}.
 */
export function buildConfirmationEmail(submission, { templateId } = {}) {
  const { first } = splitName(submission.name);

  return {
    templateId,
    params: {
      to_email: submission.email,
      "First Name": first,
      "Company Name": COMPANY_NAME,
      "Phone Number": PHONE_DISPLAY,
    },
  };
}

/**
 * Builds the EmailJS send params for the internal lead notification sent to
 * INTERNAL_NOTIFICATION_EMAIL. Variable names match the "FCRS Internal Lead"
 * template. Every captured field must be present here — this is the
 * template that must stay in sync with the schema, since a field silently
 * missing here is data loss for the sales team.
 */
export function buildInternalNotificationEmail(submission, { to, timestamp, templateId } = {}) {
  const { first, last } = splitName(submission.name);
  const notesDisplay =
    submission.notes && String(submission.notes).trim() !== "" ? String(submission.notes) : "none";

  return {
    templateId,
    params: {
      to_email: to,
      "First Name": first,
      "Last Name": last,
      "Phone Number": submission.phone,
      "Email Address": submission.email,
      "ZIP Code": submission.zip,
      "Service Requested": submission.service,
      Message: `State: ${submission.state}\n${notesDisplay}`,
      "Submission Date & Time": timestamp,
    },
  };
}
