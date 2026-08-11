/**
 * SR-27 slice 3 -- geometry-regression tests for the Knowledge page's
 * Coverage-check and Test-the-bot cards (`Console.dc.html:481-489`), using
 * this repo's established `environment: "node"` `renderToStaticMarkup`
 * pattern. Both cards must keep their honest gap-state copy verbatim
 * (G15/G16) -- only geometry is asserted here.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CoverageCheckCard, TestBotCard } from "@/app/(protected)/knowledge/upload-form";

describe("CoverageCheckCard geometry (Console.dc.html:481-485)", () => {
  it("renders 22px horizontal padding, 15px/600 title, #c9c9c9 blurb", () => {
    const html = renderToStaticMarkup(<CoverageCheckCard />);
    expect(html).toContain("px-[22px]");
    expect(html).toContain("text-[15px]");
    expect(html).toContain("font-semibold");
    expect(html).toContain("#c9c9c9");
  });

  it("renders the inner rgba(255,255,255,.08) 10px-radius panel", () => {
    const html = renderToStaticMarkup(<CoverageCheckCard />);
    expect(html).toContain("bg-white/[.08]");
    expect(html).toContain("rounded-[10px]");
  });

  it("keeps the honest empty-state copy verbatim (G15, no fabricated data)", () => {
    const html = renderToStaticMarkup(<CoverageCheckCard />);
    expect(html).toMatch(/Not available yet/);
    expect(html).toMatch(/needs a backend endpoint/);
  });
});

describe("TestBotCard geometry (Console.dc.html:486-489)", () => {
  it("renders 22px horizontal padding, 15px/600 title", () => {
    const html = renderToStaticMarkup(<TestBotCard />);
    expect(html).toContain("px-[22px]");
    expect(html).toContain("text-[15px]");
    expect(html).toContain("font-semibold");
  });

  it("renders a 42px-tall field (h-[42px]) with 10px radius", () => {
    const html = renderToStaticMarkup(<TestBotCard />);
    expect(html).toContain("h-[42px]");
    expect(html).toContain("rounded-[10px]");
  });

  it("keeps the input and Run test button disabled, never wired to the visitor endpoint (G16)", () => {
    const html = renderToStaticMarkup(<TestBotCard />);
    const disabledCount = (html.match(/disabled=""/g) ?? []).length;
    expect(disabledCount).toBe(2);
    expect(html).toMatch(/admin-authenticated query-preview endpoint/);
  });
});
