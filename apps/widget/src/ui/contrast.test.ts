import { describe, expect, it } from "vitest";

import { accentTextColor, contrastRatio, onAccentColor } from "./contrast";

const INK = "#0f172a";
const WHITE = "#ffffff";

describe("onAccentColor", () => {
  it("puts dark text on a light accent (cream) and white text on a dark one", () => {
    expect(onAccentColor("#f5e6c8")).toBe(INK);
    expect(onAccentColor("#fdf6e3")).toBe(INK);
    expect(onAccentColor("#2563eb")).toBe(WHITE); // the default blue is unchanged
    expect(onAccentColor("#111827")).toBe(WHITE);
  });
});

describe("accentTextColor", () => {
  it("keeps an accent that is already readable on white", () => {
    expect(accentTextColor("#2563eb")).toBe("#2563eb");
  });

  it("darkens a light accent until it reads on white (WCAG AA 4.5:1)", () => {
    for (const accent of ["#f5e6c8", "#fdf6e3", "#facc15", "#ffffff"]) {
      const text = accentTextColor(accent);
      expect(contrastRatio(text, WHITE)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("keeps the accent's hue when darkening (a cream turns brown, not grey)", () => {
    const text = accentTextColor("#f5e6c8");
    const red = parseInt(text.slice(1, 3), 16);
    const blue = parseInt(text.slice(5, 7), 16);
    expect(red).toBeGreaterThan(blue);
  });
});
