/**
 * Pure, client-safe presentation helpers for the dashboard chatbot hub cards
 * (SR-12). No `server-only` import, no data fetching -- these only reshape the
 * real `HubActiveConversation` fields the hub already fetched into the small
 * derived values the "Answered by chatbot" activity list and the
 * "Conversation usage" bars render. Kept in a `.ts` module (no JSX) so the
 * node-environment Vitest suite can unit-test the honesty guards directly,
 * mirroring `@/app/(protected)/conversations/presentation.ts`.
 *
 * Honesty notes (load-bearing -- CLAUDE.md §3 no-silent-fallbacks + the
 * anonymous-visitor guarantee):
 *  - No function here ever produces a person's name. Widget visitors are
 *    anonymous; the backend carries no visitor-name field. The activity row's
 *    avatar initials come ONLY from the real `conversationId` (a "C-xxxx"
 *    short form), never a fabricated name.
 *  - The usage bars are a single-period presence indicator built from the ONE
 *    real fetched total (`analytics.totals.conversations` for the currently
 *    selected period). We do NOT fabricate three independent week/month/year
 *    numbers -- only the selected period is ever fetched (see SR-12 D2 and
 *    lib/hub.ts's single `getChatbotHub({ period, bucket })` read).
 */

import type { HubActiveConversation, HubBucket, HubPeriod } from "@/lib/hub";
import type { AnalyticsOverview } from "@/lib/analytics";

export interface ActivityRow {
  /** Stable React key + the honest identity this row is derived from. */
  conversationId: string;
  /** Avatar initials derived from the conversationId short form -- NEVER a
   * person's name (visitors are anonymous). e.g. "C-9F2A" -> "9F". */
  initials: string;
  /** A short, honest label for the conversation identity (never a name). */
  identityLabel: string;
  /** The real `summary` text, or a `Started <date>` fallback exactly as the
   * prior single-row preview did when `summary` was null. */
  preview: string;
  /** The real `status` (e.g. "active"), surfaced verbatim as a badge. */
  status: string;
  /** ISO `startedAt`, for the caller to render an approximate relative time. */
  startedAt: string;
}

/** Short, human-scannable form of a conversation id for the avatar/label --
 * "C-" + the last 4 characters, uppercased. Derived purely from the real
 * `conversationId`; falls back to "C-????" for a blank id rather than
 * throwing or inventing content. */
export function conversationShortId(conversationId: string): string {
  const trimmed = conversationId.trim();
  if (trimmed.length === 0) return "C-????";
  const tail = trimmed.slice(-4).toUpperCase();
  return `C-${tail}`;
}

/** Two-character avatar initials from the conversation id short form (the
 * two significant characters of the "C-xxxx" tail). This is deliberately NOT
 * a person's initials -- there is no name to take initials from. */
export function conversationInitials(conversationId: string): string {
  const tail = conversationShortId(conversationId).replace(/^C-/, "");
  const cleaned = tail.replace(/\?/g, "");
  if (cleaned.length === 0) return "C";
  return cleaned.slice(0, 2).toUpperCase();
}

/**
 * Build up to `max` honest activity rows from the already-fetched active
 * conversations. Never pads to reach `max` (0 or 1 real items yield 0 or 1
 * rows), never fabricates a name, and preserves the exact `summary` ->
 * `Started <date>` fallback the single-row preview used.
 */
export function buildActivityRows(
  items: HubActiveConversation[],
  formatDate: (iso: string) => string,
  max = 3
): ActivityRow[] {
  return items.slice(0, max).map((item) => {
    const summary = item.summary?.trim();
    return {
      conversationId: item.conversationId,
      initials: conversationInitials(item.conversationId),
      identityLabel: conversationShortId(item.conversationId),
      preview: summary && summary.length > 0 ? summary : `Started ${formatDate(item.startedAt)}`,
      status: item.status,
      startedAt: item.startedAt,
    };
  });
}

/**
 * SR-16 D3: the gauge and the "Conversation Rate" metric card both render
 * `deflectionRate` -- and ONLY `deflectionRate`, never `fallbackRate` or
 * `groundedRate` (D2's per-widget mapping table). Centralizing the read
 * here, rather than inlining `hub.analytics.deflectionRate` at each call
 * site, gives the data-mapping tests one function to assert against on a
 * fixture where all three rates differ, so a future mis-wire (e.g. someone
 * "simplifying" a prop and grabbing `fallbackRate` by habit) fails a unit
 * test instead of shipping silently.
 */
export function answeredByChatbotRate(analytics: AnalyticsOverview): number | null {
  return analytics.deflectionRate;
}

export interface GaugeReading {
  /** `null` when the rate is undefined (zero-denominator window) -- the
   * caller MUST render "No data" with no needle, never a 0% sweep (M4/D3). */
  rate: number | null;
  /** Rounded whole-percent for display, or `null` to match `rate`. */
  percent: number | null;
  /** Full sentence for `aria-label`/screen-reader use -- never says
   * "0 percent" for a null rate. */
  label: string;
}

/** Builds the gauge's honest display reading from the real `deflectionRate`
 * (D3). A `null` rate never becomes a displayed/announced `0%` (M4). */
export function buildGaugeReading(analytics: AnalyticsOverview): GaugeReading {
  const rate = answeredByChatbotRate(analytics);
  if (rate === null) {
    return { rate: null, percent: null, label: "Answered by chatbot: No data" };
  }
  const percent = Math.round(rate * 100);
  return { rate, percent, label: `Answered by chatbot: ${percent} percent` };
}

export interface EscalationReading {
  /** `null` when there were zero conversations in the window (undefined
   * denominator) -- render "No data", never a 0% donut sweep. */
  rate: number | null;
  percent: number | null;
  escalations: number;
  handled: number;
  label: string;
}

/**
 * SR-16 D4: the Escalation Rate card/donut derives from the existing
 * `series[]` -- summed `escalations` over summed `conversations` across the
 * window, NOT a new backend aggregate. A window with zero conversations
 * (every bucket empty) yields `rate: null` ("No data"), distinguished from
 * a window with real conversations but zero escalations (`rate: 0`, a true
 * and honestly-renderable 0%).
 */
export function buildEscalationReading(analytics: AnalyticsOverview): EscalationReading {
  const escalations = analytics.series.reduce((sum, entry) => sum + entry.escalations, 0);
  const conversations = analytics.series.reduce((sum, entry) => sum + entry.conversations, 0);
  const handled = conversations - escalations;
  if (conversations === 0) {
    return { rate: null, percent: null, escalations, handled: 0, label: "Escalation rate: No data" };
  }
  const rate = escalations / conversations;
  const percent = Math.round(rate * 100);
  return { rate, percent, escalations, handled, label: `Escalation rate: ${percent} percent` };
}

/** SR-16 D4: the bucket selector on "Responses over time" offers ONLY
 * `day`/`week` (`HUB_BUCKETS`) -- `month` is deliberately absent because the
 * hub endpoint's own vocabulary does not include it (widening it is an
 * explicitly out-of-scope backend change, see the sprint's Out-of-scope
 * section). This helper exists so the omission is asserted by a unit test
 * rather than only by visual inspection of the rendered selector. */
export function isHubBucket(value: string, buckets: readonly HubBucket[]): value is HubBucket {
  return (buckets as readonly string[]).includes(value);
}

export interface UsageBar {
  /** The period label this bar/segment is tied to. */
  period: HubPeriod;
  /** Whether this is the currently selected period (the one with a real
   * fetched total). Only the selected bar carries a real number. */
  selected: boolean;
  /** Fill fraction 0..1. For the selected period this is a full presence
   * bar (1 when there is any usage, 0 when the total is 0); non-selected
   * periods render an empty track because their totals were NOT fetched --
   * we never fabricate a number for them. */
  fill: number;
}

/**
 * SR-12 D2 (fallback shape): only the currently-selected period's total is
 * ever fetched, so we build a single-period presence indicator, NOT three
 * real week/month/year numbers. The selected period's bar is filled iff its
 * real total is > 0; the other two periods render as empty tracks (a label
 * echo of the existing single `period` selection), never a fabricated value.
 */
export function buildUsageBars(
  selectedPeriod: HubPeriod,
  selectedPeriodTotal: number,
  periods: readonly HubPeriod[]
): UsageBar[] {
  return periods.map((period) => {
    const selected = period === selectedPeriod;
    return {
      period,
      selected,
      fill: selected ? (selectedPeriodTotal > 0 ? 1 : 0) : 0,
    };
  });
}
