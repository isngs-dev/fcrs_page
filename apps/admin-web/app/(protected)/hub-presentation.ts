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

import type { HubActiveConversation, HubPeriod } from "@/lib/hub";

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
