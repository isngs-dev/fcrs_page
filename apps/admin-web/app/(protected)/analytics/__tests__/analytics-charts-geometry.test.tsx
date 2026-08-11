/**
 * SR-27 slice 9 -- geometry-isolation tests for the three new Analytics
 * chart primitives (`RingGauge`, `VolumeAreaChart`, `DecisionMixDonut`),
 * verified in isolation per this repo's established `environment: "node"`
 * `renderToStaticMarkup` pattern (no jsdom).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { RingGauge } from "@/app/(protected)/analytics/ring-gauge";
import { VolumeAreaChart } from "@/app/(protected)/analytics/volume-area-chart";
import { DecisionMixDonut } from "@/app/(protected)/analytics/decision-mix-donut";
import type { AnalyticsOverview } from "@/lib/analytics";

describe("RingGauge (SR-23 spec: r=52, stroke-width 13, round cap)", () => {
  it("renders r=52 and stroke-width=13 on the progress arc", () => {
    const html = renderToStaticMarkup(<RingGauge label="Grounded rate" rate={0.72} caption="test" />);
    expect(html).toContain('r="52"');
    expect(html).toContain('stroke-width="13"');
    expect(html).toContain('stroke-linecap="round"');
  });

  it("renders the percentage centered for a real rate", () => {
    const html = renderToStaticMarkup(<RingGauge label="Grounded rate" rate={0.5} caption="test" />);
    expect(html).toContain("50%");
  });

  it("renders an explicit 'No data' state for a null rate -- NEVER a 0%-filled ring", () => {
    const html = renderToStaticMarkup(<RingGauge label="Grounded rate" rate={null} caption="test" />);
    expect(html).toMatch(/No data/i);
    expect(html).not.toContain("0%");
  });

  it("a real 0-valued rate (distinct from null) still renders a numeric 0%, not 'No data'", () => {
    const html = renderToStaticMarkup(<RingGauge label="Grounded rate" rate={0} caption="test" />);
    expect(html).toContain("0%");
    expect(html).not.toMatch(/No data/i);
  });
});

function makeOverview(seriesConversations: number[]): AnalyticsOverview {
  return {
    window: { from: "2026-07-01", to: "2026-07-08", bucket: "day" },
    totals: { conversations: 10, userTurns: 20, botTurns: 20, decidedBotTurns: 15 },
    intentDistribution: {},
    decisionDistribution: { answer: 8, escalate: 2 },
    fallbackRate: null,
    deflectionRate: 0.8,
    groundedRate: 0.9,
    schedule: { ctaConversations: 5, conversions: 2, conversionRate: 0.4 },
    series: seriesConversations.map((c, i) => ({
      bucketStart: `2026-07-0${i + 1}T00:00:00Z`,
      conversations: c,
      answers: Math.round(c * 0.8),
      escalations: Math.round(c * 0.1),
      bookings: Math.round(c * 0.05),
    })),
  };
}

describe("VolumeAreaChart (SR-23 spec: filled area + line, 3 gridlines + baseline)", () => {
  it("renders a filled area path with the reference fill color", () => {
    const html = renderToStaticMarkup(<VolumeAreaChart data={makeOverview([5, 8, 3, 10, 6])} />);
    expect(html).toContain("rgba(51,51,51,.07)");
  });

  it("renders a stroked polyline in the reference line color", () => {
    const html = renderToStaticMarkup(<VolumeAreaChart data={makeOverview([5, 8, 3])} />);
    expect(html).toContain("#333333");
  });

  it("renders at least 3 gridlines (plus baseline)", () => {
    const html = renderToStaticMarkup(<VolumeAreaChart data={makeOverview([5, 8, 3])} />);
    const lineCount = (html.match(/<line/g) ?? []).length;
    expect(lineCount).toBeGreaterThanOrEqual(3);
  });

  it("renders an honest empty state for a zero-length series", () => {
    const html = renderToStaticMarkup(<VolumeAreaChart data={makeOverview([])} />);
    expect(html).toMatch(/No time-series data/i);
  });
});

describe("DecisionMixDonut (reuses the shared Donut primitive)", () => {
  it("renders real slice labels and counts from decisionDistribution", () => {
    const html = renderToStaticMarkup(<DecisionMixDonut data={{ answer: 8, escalate: 2 }} />);
    expect(html).toMatch(/answer/);
    expect(html).toMatch(/escalate/);
    expect(html).toContain("10"); // center total
  });

  it("renders an honest empty message for an all-zero distribution, never a fabricated slice", () => {
    const html = renderToStaticMarkup(<DecisionMixDonut data={{}} />);
    expect(html).toMatch(/No data for this window/i);
  });
});
