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
  prospecting: { bg: "#eeebe3", fg: "#3d4540" },
  qualification: { bg: "#e4ece3", fg: "#1f4b3a" },
  proposal: { bg: "#f6e7dc", fg: "#9a4a1e" },
  negotiation: { bg: "#f8eed9", fg: "#855c12" },
  closed_won: { bg: "#e5efe7", fg: "#2e6a46" },
  closed_lost: { bg: "#f8e6e0", fg: "#b3452f" },
};

export function dealStageBadgeStyle(stage: string): { label: string; bg: string; fg: string } {
  const style = STAGE_BADGES[stage] ?? { bg: "#eeebe3", fg: "#3d4540" };
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
