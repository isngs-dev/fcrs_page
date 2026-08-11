/**
 * Pure presentation helper(s) for the Reports KPI row, extracted so they are
 * unit-testable without importing `page.tsx` (an async server component that
 * transitively pulls in `lib/auth`'s `requireAnyRole` and other server-only
 * modules unsuitable for this repo's `environment: "node"` vitest run).
 * Mirrors the `message-sources-presentation.ts` / `lib/leads-presentation.ts`
 * extraction pattern already used elsewhere in this app for the same reason.
 */

// SR-30 D30-10: `avgDealSize === null` means unmeasured (no closed-won deal
// carried an amount in this window) and renders "No data", NEVER "$0" -- the
// same null-is-not-zero convention as `RingGauge` and `WinRateBar`. When the
// tenant has not configured a currency (`currencyConfigured === false`), the
// number renders bare with its currency code -- never a hardcoded "$" that
// would silently assert a currency nobody configured.
export function formatDealSize(
  avgDealSize: string | null,
  currency: string,
  currencyConfigured: boolean
): string {
  if (avgDealSize === null) return "No data";
  const amount = Number(avgDealSize);
  if (!Number.isFinite(amount)) return "No data";
  const rounded = amount >= 1000 ? `${(amount / 1000).toFixed(1)}k` : amount.toLocaleString();
  if (!currencyConfigured) return `${rounded} ${currency}`;
  const symbol = currency === "USD" ? "$" : `${currency} `;
  return `${symbol}${rounded}`;
}
