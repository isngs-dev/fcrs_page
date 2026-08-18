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
  const digits = raw.replace(/\D/g, "").slice(0, 10);
  if (digits.length === 0) return "";
  if (digits.length < 4) return `(${digits}`;
  if (digits.length < 7) return `(${digits.slice(0, 3)}) ${digits.slice(3)}`;
  return `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`;
}
