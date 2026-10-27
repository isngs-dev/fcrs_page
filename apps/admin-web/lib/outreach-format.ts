/**
 * Pure helpers for the Outreach area (A8) -- importable from server and client
 * components alike (no directive), and unit-testable without mocks.
 */

/** snake_case keys -> camelCase, recursively (A8 U2's one mapping rule). */
/** snake_case -> camelCase, the same rule `camelize` applies to every response key. */
export function camelKey(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
}

export function camelize<T>(value: unknown): T {
  if (Array.isArray(value)) return value.map((v) => camelize(v)) as T;
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [camelKey(k), camelize(v)])
    ) as T;
  }
  return value as T;
}

/** U12: money is stored in micros (1 dollar = 1,000,000). */
export function microsToDollars(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "";
  return (micros / 1_000_000).toFixed(2);
}

/** Dollars typed in a form -> micros, `null` for blank, `undefined` for invalid. */
export function dollarsToMicros(text: string): number | null | undefined {
  const trimmed = text.trim().replace(/^\$/, "");
  if (!trimmed) return null;
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return undefined;
  return Math.round(Number(trimmed) * 1_000_000);
}

export function formatMoney(micros: number): string {
  return `$${microsToDollars(micros)}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** "not_interested" / "INVALID_EMAIL" / "contactCreated" -> "Not interested" / "Invalid email" / "Contact created". */
/** Why the planner dropped steps (A1 policy notes), in plain words for the person adding. */
const PLAN_REASONS: Record<string, string> = {
  CHANNEL_DISABLED: "a channel is switched off in Outreach settings",
  NO_CONSENT: "there's no recorded consent for a channel",
  CONTACT_TIMEZONE_UNKNOWN: "calls need the person's time zone",
  MAX_TOUCHES: "they've already had the most contacts allowed",
};

export function planReasonText(reasons: string[]): string {
  return reasons.map((r) => PLAN_REASONS[r] ?? humanize(r).toLowerCase()).join("; ");
}

export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  const text = value.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The composer's confidence band (A2) in plain words. */
export const BAND_LABELS: Record<string, string> = {
  answer: "Answered from your knowledge",
  clarify: "Asked a clarifying question",
  escalate: "Handed to your team",
};

/** Email list filter pills (U4), in display order. `undefined` = All. */
export const EMAIL_FILTERS: { label: string; status?: string }[] = [
  { label: "Drafts", status: "draft" },
  { label: "Sent", status: "sent" },
  { label: "Received", status: "received" },
  { label: "Failed", status: "failed" },
  { label: "All" },
];

export function isEmailFilter(value: string | undefined): boolean {
  return value !== undefined && EMAIL_FILTERS.some((f) => f.status === value);
}

/** Plain-English names for the cost kinds in `usage_costs`. */
export const COST_LABELS: Record<string, string> = {
  llm: "AI model",
  telephony: "Phone minutes",
  asr: "Speech-to-text",
  tts: "Voice",
  email: "Email",
};

/** A13: the nine outreach statuses, in pipeline order, with plain labels. */
export const OUTREACH_STATUSES: { value: string; label: string }[] = [
  { value: "new", label: "New" },
  { value: "contacted", label: "Contacted" },
  { value: "engaged", label: "Engaged" },
  { value: "qualified", label: "Qualified" },
  { value: "meeting_booked", label: "Meeting booked" },
  { value: "human_handoff", label: "With your team" },
  { value: "invalid_contact", label: "Invalid contact" },
  { value: "not_interested", label: "Not interested" },
  { value: "opted_out", label: "Opted out" },
];

/** 0.4567 -> "45.7%"; null -> "—" (no data to divide by). */
export function percent(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? "—" : `${(rate * 100).toFixed(rate < 0.1 && rate > 0 ? 1 : 0)}%`;
}
