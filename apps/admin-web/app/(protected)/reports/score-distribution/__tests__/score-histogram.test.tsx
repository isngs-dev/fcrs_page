/**
 * SR-27 slice 6 -- geometry-regression test for `ScoreHistogram`'s flip
 * from horizontal bars to vertical columns (`Console.dc.html`). Uses this
 * repo's established `environment: "node"` `renderToStaticMarkup` pattern.
 * Data logic (band math, unscored separation) is unchanged and already
 * covered indirectly via the rendered counts here.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ScoreHistogram } from "@/app/(protected)/reports/score-distribution/score-histogram";
import type { ScoreDistributionReport } from "@/lib/reports";

function makeData(overrides: Partial<ScoreDistributionReport> = {}): ScoreDistributionReport {
  return {
    window: { from: "2026-07-01T00:00:00Z", to: "2026-07-31T00:00:00Z" },
    total: 10,
    bands: { "0-19": 1, "20-39": 2, "40-59": 3, "60-79": 2, "80-100": 1 },
    unscored: 1,
    ...overrides,
  };
}

describe("ScoreHistogram vertical-column orientation (SR-27 slice 6)", () => {
  it("lays bars out in a row (flex items-end), not a stacked column list", () => {
    const html = renderToStaticMarkup(<ScoreHistogram data={makeData()} />);
    expect(html).toContain("items-end");
    expect(html).toContain("flex-1 flex-col items-center");
  });

  it("varies bar HEIGHT (not width) to encode magnitude", () => {
    const html = renderToStaticMarkup(<ScoreHistogram data={makeData()} />);
    expect(html).toMatch(/height:\d+(\.\d+)?%/);
    expect(html).not.toMatch(/width:\d+(\.\d+)?%/);
  });

  it("prints the count ABOVE each bar, and the band label below it", () => {
    const html = renderToStaticMarkup(<ScoreHistogram data={makeData()} />);
    // Every band count appears, and "Unscored" still renders as its own column.
    expect(html).toContain(">1<");
    expect(html).toContain(">2<");
    expect(html).toContain(">3<");
    expect(html).toMatch(/Unscored/);
  });

  it("keeps unscored visually separated from the five real bands (D8, not a 6th band)", () => {
    const html = renderToStaticMarkup(<ScoreHistogram data={makeData()} />);
    expect(html).toContain("border-l");
    expect(html).toContain("border-dashed");
  });

  it("renders the honest empty state for a zero-total window", () => {
    const html = renderToStaticMarkup(<ScoreHistogram data={makeData({ total: 0, bands: {}, unscored: 0 })} />);
    expect(html).toMatch(/No leads in this window/);
  });

  it("keeps the accessible sr-only table with exact values", () => {
    const html = renderToStaticMarkup(<ScoreHistogram data={makeData()} />);
    expect(html).toContain("sr-only");
    expect(html).toContain("<caption>");
  });
});
