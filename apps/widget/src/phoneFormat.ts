/**
 * Live-typing formatter for a plain US phone input -- used by LeadForm,
 * CalendlyHandoff, and ScheduleCta's confirm step, all three of which
 * collect a single US-only phone field (no country code selector; this
 * platform's visitors/leads are US-only by product decision).
 *
 * Strips every non-digit character (so letters/symbols can never be typed
 * at all, not just rejected on submit) and caps at 10 digits -- a US phone
 * number's area code + exchange + line number -- formatting progressively
 * as the visitor types: "7" -> "(7", "7867" -> "(786) 7",
 * "7867756768" -> "(786) 775-6768". Extra digits beyond the 10th are
 * silently dropped rather than accepted and left unformatted.
 */
export function formatUsPhoneInput(raw: string): string {
  // A number with a country code ("+91 98765 43210") is left as typed -- the US format cut
  // an Indian number to 10 digits (live 2026-11-20).
  if (raw.trimStart().startsWith("+")) return raw.replace(/[^\d+\s()-]/g, "").slice(0, 24);
  const digits = raw.replace(/\D/g, "").slice(0, 10);
  if (digits.length === 0) return "";
  if (digits.length < 4) return `(${digits}`;
  if (digits.length < 7) return `(${digits.slice(0, 3)}) ${digits.slice(3)}`;
  return `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`;
}

/** The phone fields' country-code picker (owner, 2026-11-24): a US number rings from Telnyx,
 * an Indian one from the India route -- the server reads the +country code. */
export const DIAL_CODES = [
  { code: "+1", label: "US +1" },
  { code: "+91", label: "IN +91" },
] as const;
export type DialCode = (typeof DIAL_CODES)[number]["code"];

/** Live formatting for the picked country: US "(786) 775-6768", India "98765 43210". */
export function formatPhoneInput(raw: string, dial: DialCode): string {
  if (dial === "+1" || raw.trimStart().startsWith("+")) return formatUsPhoneInput(raw);
  const digits = raw.replace(/\D/g, "").slice(0, 10);
  return digits.length > 5 ? `${digits.slice(0, 5)} ${digits.slice(5)}` : digits;
}

/** The number as sent: an Indian one gets +91 in front; a US one goes as before. */
export function withDialCode(phone: string, dial: DialCode): string {
  const p = phone.trim();
  return dial === "+1" || p === "" || p.startsWith("+") ? p : `${dial}${p.replace(/\D/g, "")}`;
}

/** A number we already have, split back into the picker and the field. */
export function splitDialCode(phone: string): { dial: DialCode; number: string } {
  const p = phone.trim();
  const digits = p.replace(/\D/g, "");
  if (p.startsWith("+91") && digits.length === 12) return { dial: "+91", number: formatPhoneInput(digits.slice(2), "+91") };
  if (p.startsWith("+1") && digits.length === 11) return { dial: "+1", number: formatUsPhoneInput(digits.slice(1)) };
  return { dial: "+1", number: phone };
}
