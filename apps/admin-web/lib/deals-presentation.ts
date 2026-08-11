/**
 * Client-safe presentation helpers for deals (SR-18 D6). Pure functions
 * only -- no `server-only` import -- so both the server-rendered
 * `deals-table.tsx` and the client `deals-board.tsx`/`deal-drawer.tsx` can
 * import them without pulling `lib/deals.ts`'s `server-only` boundary in.
 *
 * D6 is the single hardest rule this module exists to keep honest:
 * `amount === null` MUST render as "Not quoted", never "$0"/"0"/"—", and
 * this function never performs numeric arithmetic on `amount` -- it is
 * concatenated as the string it already is (see `lib/deals.ts`'s header
 * comment for why `amount` is a `string | null` in the first place).
 */

const STAGE_LABELS: Record<string, string> = {
  prospecting: "Prospecting",
  qualification: "Qualification",
  proposal: "Proposal",
  negotiation: "Negotiation",
  closed_won: "Closed Won",
  closed_lost: "Closed Lost",
};

export function dealStageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

const STAGE_BADGES: Record<string, { bg: string; fg: string }> = {
  prospecting: { bg: "#efeee6", fg: "var(--ink-2)" },
  qualification: { bg: "#efeee6", fg: "#333333" },
  proposal: { bg: "#efeee6", fg: "#404040" },
  negotiation: { bg: "#fdf6e3", fg: "#8a6416" },
  closed_won: { bg: "#eaf3ec", fg: "#3f7d57" },
  closed_lost: { bg: "#f6e3df", fg: "#a24b4b" },
};

export function dealStageBadgeStyle(stage: string): { label: string; bg: string; fg: string } {
  const style = STAGE_BADGES[stage] ?? { bg: "#ecece5", fg: "var(--ink-2)" };
  return { label: dealStageLabel(stage).toUpperCase(), ...style };
}

/**
 * D6 (LOCKED): `null` -> "Not quoted", NEVER "$0"/"0"/"—" (a real $0 deal
 * and an unquoted deal must stay visually distinguishable -- SR-9.4 D7). No
 * arithmetic, no `parseFloat`, no `Number()` -- `amount` is concatenated
 * as-is with its row's own `currency` code (never a global assumption).
 */
export function formatDealAmount(amount: string | null, currency: string): string {
  if (amount === null) return "Not quoted";
  return `${amount} ${currency}`;
}

export function formatDealDate(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
