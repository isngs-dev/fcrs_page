/**
 * SR-30 S5 -- coverage for the Reports "Avg deal" KPI tile's null/currency
 * rules (D30-10) and the funnel's inline conversion-rate labels (D30-11),
 * verified per this repo's established `environment: "node"`
 * `renderToStaticMarkup` pattern (see
 * `analytics/__tests__/analytics-charts-geometry.test.tsx`) -- no jsdom.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { formatDealSize } from "@/app/(protected)/reports/reports-kpi-presentation";
import { FunnelSteps } from "@/app/(protected)/reports/funnel/funnel-steps";
import type { FunnelReport } from "@/lib/reports";

describe("formatDealSize (D30-10)", () => {
  it("renders 'No data' for a null avgDealSize, never $0", () => {
    expect(formatDealSize(null, "USD", true)).toBe("No data");
  });

  it("renders a real zero as a numeric zero, not 'No data'", () => {
    expect(formatDealSize("0", "USD", true)).not.toBe("No data");
    expect(formatDealSize("0", "USD", true)).toContain("0");
  });

  it("renders no currency symbol when currencyConfigured is false", () => {
    const value = formatDealSize("4800", "USD", false);
    expect(value).not.toContain("$");
    expect(value).toContain("USD");
  });

  it("renders a $ symbol for USD when currencyConfigured is true", () => {
    expect(formatDealSize("4800", "USD", true)).toContain("$");
  });

  it("abbreviates large amounts with a k suffix", () => {
    expect(formatDealSize("4800", "USD", true)).toMatch(/4\.8k/);
  });
});

function makeFunnelReport(steps: { stage: string; count: number; dropOffRate: number | null }[]): FunnelReport {
  return {
    window: { from: "2026-07-01", to: "2026-07-08" },
    steps,
    disqualifiedCount: 0,
    overallConversionRate: 0.5,
  };
}

describe("FunnelSteps inline rate labels (D30-11)", () => {
  it("renders a conversion-rate label (1 - dropOffRate) when dropOffRate is real", () => {
    const html = renderToStaticMarkup(
      <FunnelSteps
        data={makeFunnelReport([
          { stage: "captured", count: 100, dropOffRate: null },
          { stage: "qualified", count: 58, dropOffRate: 0.42 },
        ])}
      />
    );
    expect(html).toContain("58% conversion");
  });

  it("suppresses the inline label when dropOffRate is null (first step) -- never renders a fake 100%", () => {
    const html = renderToStaticMarkup(
      <FunnelSteps
        data={makeFunnelReport([{ stage: "captured", count: 100, dropOffRate: null }])}
      />
    );
    expect(html).not.toContain("100% conversion");
  });

  it("keeps the raw drop-off rate in the sr-only table (no information lost)", () => {
    const html = renderToStaticMarkup(
      <FunnelSteps
        data={makeFunnelReport([
          { stage: "captured", count: 100, dropOffRate: null },
          { stage: "qualified", count: 58, dropOffRate: 0.42 },
        ])}
      />
    );
    expect(html).toContain("42%");
  });
});
