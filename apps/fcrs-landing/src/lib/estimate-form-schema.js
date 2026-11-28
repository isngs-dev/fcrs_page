// Hand-encoded mirror of the backend's shared zod schema
// (backend/src/schema/estimate-request.js). Astro's static frontend has no
// runtime access to backend/, so this module re-declares the identical field
// list, types, enums, and required-ness so parity can be verified by
// inspection. See CLAUDE.md "Field parity across forms that feed one record"
// and the frontend SKILL's "Forms" section.
//
// Keep this in lockstep with backend/src/schema/estimate-request.js:
// - same six required fields: name, phone, email, service, state, zip
// - same two optional fields: date, notes
// - same two enums, same member lists, same order
//
// This module does NOT talk to the network. It only provides:
//   1. FIELD_DEFS - the field list/types/required flags, single source for
//      building the form UI and for the client-side pre-flight checks.
//   2. validateEstimateForm(values) - client-side UX validation only. The
//      server round-trip is always authoritative; this never replaces it.

export const SERVICE_OPTIONS = [
  "Residential Roofing",
  "Commercial Roofing",
  "Emergency Repair",
];

export const STATE_OPTIONS = ["Alabama", "Georgia"];

/**
 * @typedef {Object} FieldDef
 * @property {string} name
 * @property {"string"|"email"|"enum"|"date"|"notes"} type
 * @property {boolean} required
 * @property {string[]} [options]
 */

/** @type {FieldDef[]} */
export const FIELD_DEFS = [
  { name: "name", type: "string", required: true },
  { name: "phone", type: "string", required: true },
  { name: "email", type: "email", required: true },
  { name: "service", type: "enum", required: true, options: SERVICE_OPTIONS },
  { name: "state", type: "enum", required: true, options: STATE_OPTIONS },
  { name: "zip", type: "string", required: true },
  { name: "date", type: "date", required: false },
  { name: "notes", type: "notes", required: false },
];

export const REQUIRED_FIELD_NAMES = FIELD_DEFS.filter((f) => f.required).map(
  (f) => f.name
);

// Same permissive email pattern shape as a client-side pre-flight; the
// backend's zod .email() is authoritative on the server round-trip.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Client-side validation only, for responsiveness. Mirrors the backend's
 * required-field and enum-membership checks but produces the same shape of
 * flat field->message error map the server returns on 400, so the same
 * inline-error rendering code path can be reused for both.
 *
 * @param {Record<string, string|undefined>} values
 * @returns {Record<string, string>} flat map of field name -> message (empty if valid)
 */
export function validateEstimateForm(values) {
  /** @type {Record<string, string>} */
  const errors = {};

  const name = (values.name ?? "").trim();
  if (!name) errors.name = "Full name is required.";

  const phone = (values.phone ?? "").trim();
  if (!phone) errors.phone = "Phone number is required.";

  const email = (values.email ?? "").trim();
  if (!email) {
    errors.email = "Email address is required.";
  } else if (!EMAIL_RE.test(email)) {
    errors.email = "Enter a valid email address.";
  }

  const service = values.service ?? "";
  if (!service) {
    errors.service = "Select a service.";
  } else if (!SERVICE_OPTIONS.includes(service)) {
    errors.service = "Select a service.";
  }

  const state = values.state ?? "";
  if (!state) {
    errors.state = "Select a state.";
  } else if (!STATE_OPTIONS.includes(state)) {
    errors.state = "Select a state.";
  }

  const zip = (values.zip ?? "").trim();
  if (!zip) errors.zip = "Zip code is required.";

  // date and notes are optional — no checks.

  return errors;
}

/**
 * @param {Record<string, string>} errors
 */
export function isValid(errors) {
  return Object.keys(errors).length === 0;
}

// The call-consent box's exact text. The chatbot platform records this as the
// person's written consent for AI calls, so it must match, word for word, the
// "call-consent wording" saved in the admin console (Outreach -> Lead intake).
// Change both together.
export const CALL_CONSENT_WORDING =
  "Yes, ISN Roofing may call me right away at the number above about my estimate, " +
  "including with an AI assistant. Calls may be recorded. Consent is not a " +
  "condition of purchase; I can say stop at any time.";

/**
 * The body POSTed to the platform's form intake (/public/intake/forms/{token}).
 * Contact fields use the names intake reads; the rest go in as labelled answers
 * (a label containing "service" / "message" is what the AI caller reads back).
 *
 * @param {Record<string, string|undefined>} values
 * @param {{ callConsent: boolean, timezone?: string, countryCode: string }} opts
 */
export function toIntakePayload(values, { callConsent, timezone, countryCode }) {
  return {
    form: "ISN Roofing estimate",
    name: values.name ?? "",
    phone: values.phone ?? "",
    country_code: countryCode,
    email: values.email ?? "",
    "Service Needed": values.service ?? "",
    State: values.state ?? "",
    "Zip Code": values.zip ?? "",
    "Preferred Date / Time": values.date ?? "",
    Message: values.notes ?? "",
    call_consent: callConsent ? "yes" : "no",
    ...(timezone ? { timezone } : {}),
  };
}
