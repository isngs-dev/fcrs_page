/**
 * Live-typing formatter for a plain (non-country-selector) US phone input --
 * used by LeadForm and CalendlyHandoff, which both collect a single phone
 * field with no country code selector (unlike ScheduleCta's confirm step,
 * which has its own international country-code dropdown via
 * countryCodes.ts and is deliberately left alone here).
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
