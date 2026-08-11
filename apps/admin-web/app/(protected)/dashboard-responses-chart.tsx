"use client";

/**
 * SR-23 scope item 1 -- the diverging ("butterfly") plot inside the
 * "Responses over time" card, extracted as the page's ONLY client island so
 * the dashboard itself stays a server component (SR-23 handoff, "extract just
 * the chart into its own client island rather than converting the whole
 * page").
 *
 * Geometry is read verbatim from the reference artboard
 * `Dashboard.dc.html:161-187`:
 *  - plot area is exactly `height:195px`, `position:relative`
 *  - six gridlines at top 0 / 39 / 78 / 117 / 156 and bottom 0; the 117px one
 *    is `#cdcdcd` (the shared midline the two series split around), the other
 *    five are the lighter `#e6e6e6`
 *  - one relative slot per bucket, `flex:1`, `gap:3px`
 *  - "Answered" bar: `bottom:78px` (== 195-117, i.e. its bottom edge sits ON
 *    the midline), grows UP, `border-radius:3px 3px 0 0`, `#333333`
 *  - "Escalated" bar: `top:117px` (the same midline), grows DOWN,
 *    `border-radius:0 0 3px 3px`, `#878787`
 *  - both inset `left:28%; right:28%` so the bar is narrower than its slot
 * This is a diverging chart, NOT the grouped/side-by-side pair that shipped
 * before -- the two series never share a bottom baseline.
 *
 * Honesty (CLAUDE.md §3, no silent fallbacks -- extended to axis labels and
 * tooltip content by the SR-23 handoff):
 *  - The reference mock plots 24 daily points across a year and therefore
 *    labels its x-axis by MONTH. This app's series is `day`- or `week`-
 *    bucketed (`HUB_BUCKETS`), so month labels would misrepresent the data.
 *    We match the reference's SHAPE (evenly-spaced sampled labels rather than
 *    one label per bar) while keeping each label the real `bucketStart` of
 *    the bucket it sits under -- see `sampleAxisLabels`.
 *  - The up/down heights scale against the real max across BOTH series, so
 *    the two halves stay directly comparable; a zero value renders zero
 *    height, never a decorative minimum stub.
 *  - The tooltip prints the real bucket date and the two real counts.
 */

import { useState } from "react";
import type { HubBucket } from "@/lib/hub";

/** Reference `Dashboard.dc.html:161` -- the plot box. */
const PLOT_HEIGHT = 195;
/** Reference `Dashboard.dc.html:165` -- the shared midline, measured from the
 * top of the plot. `PLOT_HEIGHT - MIDLINE` (78) is the same line measured
 * from the bottom, which is what the upward bar's `bottom` uses. */
const MIDLINE = 117;
const UP_SPACE = MIDLINE;
const DOWN_SPACE = PLOT_HEIGHT - MIDLINE;
/** Reference `Dashboard.dc.html:162-167` -- the five light gridlines plus the
 * distinct midline. */
const LIGHT_GRIDLINES = [0, 39, 78, 156];

export interface ResponsesChartBucket {
  bucketStart: string;
  answers: number;
  escalations: number;
  /** Real, already-formatted bucket date for the tooltip heading. */
  label: string;
  /** Real, already-formatted short label for the x-axis. */
  shortLabel: string;
}

/**
 * Evenly-spaced axis labels, matching the reference's "12 labels under 24
 * bars" shape without inventing month names. Every returned label is the
 * REAL label of the bucket at that index -- we only choose WHICH buckets get
 * a printed label, never what it says. With few buckets every one is
 * labelled, exactly as the shipped chart did.
 */
export function sampleAxisLabels<T>(items: T[], maxLabels = 12): (T | null)[] {
  if (items.length <= maxLabels) return items;
  const step = Math.ceil(items.length / maxLabels);
  return items.map((item, index) => (index % step === 0 ? item : null));
}

export function ResponsesOverTimeChart({
  buckets,
  bucket,
}: {
  buckets: ResponsesChartBucket[];
  bucket: HubBucket;
}) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const max = Math.max(...buckets.flatMap((entry) => [entry.answers, entry.escalations]), 1);
  const axisLabels = sampleAxisLabels(buckets);

  return (
    <div className="mt-3 flex gap-2.5">
      {/* Reference Dashboard.dc.html:158-159 -- 30px axis gutter, 9px muted
          type, 100%..0% top to bottom. */}
      <div
        aria-hidden
        className="flex h-[195px] w-[30px] flex-none flex-col items-end justify-between text-[9px] text-muted-foreground"
      >
        <span>100%</span>
        <span>80%</span>
        <span>60%</span>
        <span>40%</span>
        <span>20%</span>
        <span>0%</span>
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div
          className="relative"
          style={{ height: PLOT_HEIGHT }}
          onMouseLeave={() => setActiveIndex(null)}
        >
          {LIGHT_GRIDLINES.map((top) => (
            <div
              key={top}
              aria-hidden
              className="pointer-events-none absolute inset-x-0 border-t border-dashed border-[#e6e6e6]"
              style={{ top }}
            />
          ))}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 bottom-0 border-t border-dashed border-[#e6e6e6]"
          />
          {/* The midline: visually distinct (--border, #cdcdcd) because it is
              the zero line both series diverge from, not a decorative rule. */}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 border-t border-dashed border-border"
            style={{ top: MIDLINE }}
          />

          <div className="absolute inset-0 flex items-stretch gap-[3px]">
            {buckets.map((entry, index) => {
              const upHeight = (entry.answers / max) * UP_SPACE;
              const downHeight = (entry.escalations / max) * DOWN_SPACE;
              return (
                <button
                  key={entry.bucketStart}
                  type="button"
                  aria-label={`${entry.label}: ${entry.answers} answered, ${entry.escalations} escalated`}
                  onMouseEnter={() => setActiveIndex(index)}
                  onFocus={() => setActiveIndex(index)}
                  onBlur={() => setActiveIndex(null)}
                  className="relative min-w-0 flex-1 cursor-default focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                >
                  <span
                    aria-hidden
                    className="absolute rounded-t-[3px] bg-primary"
                    style={{ left: "28%", right: "28%", bottom: DOWN_SPACE, height: upHeight }}
                  />
                  <span
                    aria-hidden
                    className="absolute rounded-b-[3px] bg-muted-foreground"
                    style={{ left: "28%", right: "28%", top: MIDLINE, height: downHeight }}
                  />
                </button>
              );
            })}
          </div>

          {activeIndex !== null && buckets[activeIndex] ? (
            <>
              {/* Reference Dashboard.dc.html:176 -- the dashed marker rule
                  through the hovered bucket. */}
              <div
                aria-hidden
                className="pointer-events-none absolute inset-y-0 border-l border-dashed border-muted-foreground"
                style={{ left: `${((activeIndex + 0.5) / buckets.length) * 100}%` }}
              />
              <div
                aria-hidden
                className="pointer-events-none absolute top-0 z-2 -translate-x-1/2 rounded-[9px] bg-[var(--ink-2)] px-2.5 py-[7px] text-[10px] leading-[1.35] whitespace-nowrap text-white shadow-[0_6px_18px_rgba(28,27,25,.22)]"
                style={{
                  left: `clamp(0px, ${((activeIndex + 0.5) / buckets.length) * 100}%, 100%)`,
                }}
              >
                <div className="mb-1 font-semibold">{buckets[activeIndex].label}</div>
                <div className="flex items-center gap-1.5">
                  <span className="size-2 rounded-full bg-[#333333]" />
                  Answered · {buckets[activeIndex].answers}
                </div>
                <div className="mt-0.5 flex items-center gap-1.5">
                  <span className="size-2 rounded-full bg-[#878787]" />
                  Escalated · {buckets[activeIndex].escalations}
                </div>
              </div>
            </>
          ) : null}
        </div>

        <div aria-hidden className="flex gap-[3px]">
          {axisLabels.map((entry, index) => (
            <div
              key={buckets[index]?.bucketStart ?? index}
              className="min-w-0 flex-1 truncate text-center text-[10px] text-muted-foreground"
            >
              {entry?.shortLabel ?? ""}
            </div>
          ))}
        </div>
      </div>

      <table className="sr-only">
        <caption>Answered and escalated responses per {bucket}</caption>
        <thead>
          <tr>
            <th scope="col">Bucket</th>
            <th scope="col">Answered</th>
            <th scope="col">Escalated</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((entry) => (
            <tr key={entry.bucketStart}>
              <td>{entry.label}</td>
              <td>{entry.answers}</td>
              <td>{entry.escalations}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
