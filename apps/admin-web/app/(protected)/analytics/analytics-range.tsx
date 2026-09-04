/**
 * Range + bucket control (S13.5.md decision 5), styled to the reference's
 * segmented pill toggle (7d / 30d / 90d / Custom). A plain GET `<form>`,
 * no `select` primitive dependency (decision 7), mirroring
 * `leads/leads-filter.tsx`. Submitting navigates to
 * `/analytics?range=...&bucket=...[&from=...&to=...]`, which re-runs the
 * server component.
 *
 * Picking a range pill or a bucket auto-submits the form (`"use client"` +
 * `requestSubmit()` on change) instead of requiring a separate "Apply"
 * click: users reported that clicking a preset pill appeared to do nothing,
 * since the dashboard only updates once the form is actually submitted.
 * The "Apply" button stays -- it's still how a custom `from`/`to` pair
 * gets submitted, since auto-submitting after typing only one of the two
 * date fields would fetch a half-finished range.
 *
 * The visual segmented control is built from radio inputs styled as pills
 * (native `<input type="radio">` + `<label>`) so keyboard/screen reader
 * users get real radio-group semantics instead of a div soup. The "Custom"
 * option reveals two native `<input type="date">` fields; both date inputs
 * are always present in the DOM (not conditionally toggled) and are
 * ignored server-side by `resolveAnalyticsQuery` unless `range=custom` is
 * actually submitted -- so picking a custom date range while a preset pill
 * is still checked has no unexpected effect.
 */
"use client";

import type { ChangeEvent } from "react";
import {
  ANALYTICS_BUCKETS,
  ANALYTICS_RANGES,
  CUSTOM_RANGE_KEY,
  type AnalyticsBucket,
  type AnalyticsRangeKey,
} from "@/lib/analytics";

const BUCKET_LABELS: Record<AnalyticsBucket, string> = {
  day: "Day",
  week: "Week",
  month: "Month",
};

/**
 * `basePath` (S13.7): the per-client analytics screen passes
 * `/clients/{tenantId}/analytics` so the range/bucket form stays on that
 * same tenant-scoped route instead of the implicit `/analytics`. Defaults to
 * `/analytics`, preserving the existing CLIENT_ADMIN/AGENT behavior.
 */
export function AnalyticsRange({
  currentRange,
  currentBucket,
  currentFrom,
  currentTo,
  basePath = "/analytics",
}: {
  currentRange: AnalyticsRangeKey;
  currentBucket: AnalyticsBucket;
  currentFrom?: string;
  currentTo?: string;
  basePath?: string;
}) {
  const isCustom = currentRange === CUSTOM_RANGE_KEY;

  function submitOnChange(event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void {
    event.currentTarget.form?.requestSubmit();
  }

  return (
    <form
      action={basePath}
      method="get"
      className="flex flex-wrap items-end gap-3"
      aria-label="Analytics date range and bucket"
    >
      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs font-medium text-muted-foreground">Date range</legend>
        <div className="flex overflow-hidden rounded-[9px] border border-border text-xs font-semibold">
          {ANALYTICS_RANGES.map((range) => (
            <label
              key={range.key}
              className="cursor-pointer px-3.5 py-[7px] text-[var(--ink-2)] transition-colors has-checked:bg-foreground has-checked:text-white hover:has-[:not(:checked)]:bg-secondary focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-foreground"
            >
              <input
                type="radio"
                name="range"
                value={range.key}
                defaultChecked={currentRange === range.key}
                onChange={submitOnChange}
                className="sr-only"
              />
              {range.label.replace("Last ", "")}
            </label>
          ))}
          <label className="cursor-pointer border-l border-border px-3.5 py-[7px] text-[var(--ink-2)] transition-colors has-checked:bg-foreground has-checked:text-white hover:has-[:not(:checked)]:bg-secondary focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-foreground">
            <input
              type="radio"
              name="range"
              value={CUSTOM_RANGE_KEY}
              defaultChecked={isCustom}
              onChange={submitOnChange}
              className="sr-only"
            />
            Custom
          </label>
        </div>
      </fieldset>

      <div className="flex flex-col gap-1">
        <label htmlFor="from" className="text-xs font-medium text-muted-foreground">
          Custom from
        </label>
        <input
          type="date"
          id="from"
          name="from"
          defaultValue={currentFrom}
          className="h-8 rounded-[9px] border border-border bg-white px-2.5 py-1 text-sm text-foreground outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-foreground"
        />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="to" className="text-xs font-medium text-muted-foreground">
          Custom to
        </label>
        <input
          type="date"
          id="to"
          name="to"
          defaultValue={currentTo}
          className="h-8 rounded-[9px] border border-border bg-white px-2.5 py-1 text-sm text-foreground outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-foreground"
        />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="bucket" className="text-xs font-medium text-muted-foreground">
          Bucket
        </label>
        <select
          id="bucket"
          name="bucket"
          defaultValue={currentBucket}
          onChange={submitOnChange}
          className="h-8 rounded-[9px] border border-border bg-white px-2.5 py-1 text-sm text-foreground outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-foreground"
        >
          {ANALYTICS_BUCKETS.map((bucket) => (
            <option key={bucket} value={bucket}>
              {BUCKET_LABELS[bucket]}
            </option>
          ))}
        </select>
      </div>
      {/* SR-15 D1: the ink-filled Apply button's citron text is deleted and
          re-decided to white -- matches the shell's identical re-decision
          for dark-filled buttons/pills. */}
      <button
        type="submit"
        className="h-8 rounded-[9px] bg-foreground px-3.5 text-sm font-semibold text-white transition-colors hover:bg-[var(--ink-2)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-foreground"
      >
        Apply
      </button>
    </form>
  );
}
