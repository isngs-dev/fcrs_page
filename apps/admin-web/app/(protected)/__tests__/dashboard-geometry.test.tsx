/**
 * SR-23 -- geometry regression tests for the Dashboard.
 *
 * These exist because SR-22 passed build/lint/typecheck/test while the chart
 * SHAPES were still wrong: nothing asserted geometry, only colors. Each test
 * below pins a number read verbatim out of the reference artboard
 * `knowledge_base/ui and crm inspiration/user chatbot/
 * chatbot-crm-dashboard-redesign/project/Dashboard.dc.html`, so a future
 * "simplification" back to grouped-from-baseline bars fails here instead of
 * shipping.
 *
 * Uses `react-dom/server`'s static markup, the pattern already established by
 * this repo's `.test.tsx` suites (vitest runs `environment: "node"`; no jsdom
 * or @testing-library is a dependency and CLAUDE.md §4 forbids adding one).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ResponsesOverTimeChart,
  sampleAxisLabels,
  type ResponsesChartBucket,
} from "@/app/(protected)/dashboard-responses-chart";
import { activeGaugeTickCount } from "@/app/(protected)/dashboard-hero";

function makeBuckets(count: number): ResponsesChartBucket[] {
  return Array.from({ length: count }, (_, index) => ({
    bucketStart: `2026-07-${String(index + 1).padStart(2, "0")}T00:00:00.000Z`,
    answers: 10 + index,
    escalations: 4 + index,
    label: `Day ${index + 1}`,
    shortLabel: `D${index + 1}`,
  }));
}

describe("ResponsesOverTimeChart -- diverging geometry (Dashboard.dc.html:161-175)", () => {
  const markup = renderToStaticMarkup(
    <ResponsesOverTimeChart buckets={makeBuckets(6)} bucket="day" />
  );

  it("renders a 195px plot area, the reference's exact plot height", () => {
    expect(markup).toContain("height:195px");
  });

  it("anchors the Answered bars to the midline from BELOW (bottom:78px), not to a bottom baseline", () => {
    // 78 == 195 - 117: the up-bar's bottom edge sits ON the midline.
    expect(markup).toContain("bottom:78px");
  });

  it("anchors the Escalated bars to the SAME midline from above (top:117px), growing downward", () => {
    expect(markup).toContain("top:117px");
  });

  it("insets both bars to left:28%/right:28% so a bar is narrower than its slot", () => {
    expect(markup).toContain("left:28%");
    expect(markup).toContain("right:28%");
  });

  it("scales up and down heights against the max across BOTH series", () => {
    // Buckets 0..5: answers 10..15, escalations 4..9 -> max 15.
    // Last bucket's up bar = 15/15 * 117 = 117px; its down bar = 9/15 * 78 = 46.8px.
    expect(markup).toContain("height:117px");
    expect(markup).toContain("height:46.8px");
  });

  it("renders exactly one up-bar and one down-bar per bucket (2 per bucket, never grouped side-by-side)", () => {
    const upBars = markup.match(/bottom:78px/g) ?? [];
    const downBars = markup.match(/top:117px/g) ?? [];
    expect(upBars).toHaveLength(6);
    // 6 bars + the 1 midline gridline, which shares the 117px coordinate.
    expect(downBars.length).toBeGreaterThanOrEqual(6);
  });

  it("draws the midline distinctly from the five lighter dashed gridlines", () => {
    // Light gridlines are #e6e6e6; the midline uses --border (#cdcdcd).
    expect(markup).toContain("border-[#e6e6e6]");
    expect(markup).toContain("border-border");
  });

  it("renders the left y-axis percentage column (100%..0%) the shipped chart lacked", () => {
    for (const label of ["100%", "80%", "60%", "40%", "20%", "0%"]) {
      expect(markup).toContain(label);
    }
  });

  it("keeps the accessible data table with the real per-bucket values", () => {
    expect(markup).toContain("Day 1");
    expect(markup).toContain("<caption>");
  });

  it("renders a zero value as zero height -- no decorative minimum stub", () => {
    const zeroMarkup = renderToStaticMarkup(
      <ResponsesOverTimeChart
        buckets={[
          {
            bucketStart: "2026-07-01T00:00:00.000Z",
            answers: 0,
            escalations: 0,
            label: "Day 1",
            shortLabel: "D1",
          },
        ]}
        bucket="day"
      />
    );
    // React serializes a unitless 0 as `height:0`, not `height:0px`.
    expect(zeroMarkup).toContain("bottom:78px;height:0");
    expect(zeroMarkup).toContain("top:117px;height:0");
  });
});

describe("sampleAxisLabels -- honest axis density (SR-23 handoff)", () => {
  it("labels every bucket when the count is small (no forced monthly labels)", () => {
    const items = makeBuckets(7);
    expect(sampleAxisLabels(items)).toHaveLength(7);
    expect(sampleAxisLabels(items).every((entry) => entry !== null)).toBe(true);
  });

  it("thins to evenly-spaced labels when buckets are dense, matching the reference SHAPE", () => {
    const items = makeBuckets(30);
    const labels = sampleAxisLabels(items);
    // One slot per bucket is preserved (alignment), but only every Nth prints.
    expect(labels).toHaveLength(30);
    const printed = labels.filter((entry) => entry !== null);
    expect(printed.length).toBeLessThanOrEqual(12);
    expect(printed.length).toBeGreaterThan(1);
  });

  it("every printed label is a REAL bucket, never a synthesized month name", () => {
    const items = makeBuckets(30);
    for (const entry of sampleAxisLabels(items)) {
      if (entry !== null) expect(items).toContain(entry);
    }
  });
});

describe("activeGaugeTickCount -- hero gauge (Dashboard.dc.html renderVals, lines 261-266)", () => {
  it("lights round(26 * rate) ticks, matching the reference formula exactly", () => {
    // The artboard's own worked example: activePct 0.72 over 26 ticks -> 19.
    expect(activeGaugeTickCount(0.72)).toBe(19);
  });

  it("lights every tick at 100% and none at a true 0%", () => {
    expect(activeGaugeTickCount(1)).toBe(26);
    expect(activeGaugeTickCount(0)).toBe(0);
  });

  it("lights zero ticks for a null rate -- never treated as a real 0% reading (M4)", () => {
    expect(activeGaugeTickCount(null)).toBe(0);
  });
});
