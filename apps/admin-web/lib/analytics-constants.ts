/**
 * Pure constants/types shared between `lib/analytics.ts` (server-only, has
 * the actual fetch) and `analytics/analytics-range.tsx` (a client component,
 * since it auto-submits on change). Split out because a module tagged
 * `import "server-only"` can't be imported from a Client Component at all --
 * even for symbols that don't touch the network -- so these had to move out
 * of `lib/analytics.ts` for `analytics-range.tsx` to import them.
 */
export const ANALYTICS_BUCKETS = ["day", "week", "month"] as const;

export type AnalyticsBucket = (typeof ANALYTICS_BUCKETS)[number];

export const ANALYTICS_RANGES = [
  { key: "7d", label: "Last 7 days", days: 7 },
  { key: "30d", label: "Last 30 days", days: 30 },
  { key: "90d", label: "Last 90 days", days: 90 },
] as const;

export const CUSTOM_RANGE_KEY = "custom" as const;

export type AnalyticsRangeKey = (typeof ANALYTICS_RANGES)[number]["key"] | typeof CUSTOM_RANGE_KEY;

export const DEFAULT_RANGE_KEY: AnalyticsRangeKey = "30d";
export const DEFAULT_BUCKET: AnalyticsBucket = "day";
