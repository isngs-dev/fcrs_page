/**
 * SR-27 slice 0 -- geometry-isolation test for the shared `SegmentedControl`
 * primitive, verified in isolation BEFORE any consuming page (Leads
 * Table/Board toggle, Conversations status filter) is built against it.
 * Recipe source: `Console.dc.html:46-48`.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SegmentedControl } from "@/components/admin/segmented-control";

describe("SegmentedControl (Console.dc.html:46-48 .seg recipe)", () => {
  it("renders the cream track with 3px padding and 10px radius", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl
        ariaLabel="Test"
        items={[{ key: "a", label: "A", href: "/a", active: true }]}
      />
    );
    expect(html).toContain("bg-secondary");
    expect(html).toContain("p-[3px]");
    expect(html).toContain("rounded-[10px]");
  });

  it("renders each item at 32px height (h-8) with 8px radius (rounded-lg)", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl
        ariaLabel="Test"
        items={[{ key: "a", label: "A", href: "/a", active: true }]}
      />
    );
    expect(html).toContain("h-8");
    expect(html).toContain("rounded-lg");
  });

  it("marks the active item near-black bg + #fbfaf7 text, with aria-current", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl
        ariaLabel="Test"
        items={[
          { key: "a", label: "A", href: "/a", active: true },
          { key: "b", label: "B", href: "/b", active: false },
        ]}
      />
    );
    expect(html).toContain("background:#333333");
    expect(html).toContain("color:#fbfaf7");
    expect(html).toMatch(/aria-current="true"/);
  });

  it("renders inactive items in muted-foreground, no aria-current", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl
        ariaLabel="Test"
        items={[{ key: "a", label: "A", href: "/a", active: false }]}
      />
    );
    expect(html).toContain("var(--muted-foreground)");
    expect(html).not.toMatch(/aria-current/);
  });

  it("renders each item as a real navigable <a> (server-renderable, URL-driven)", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl
        ariaLabel="Test"
        items={[{ key: "a", label: "A", href: "/somewhere", active: true }]}
      />
    );
    expect(html).toMatch(/<a[^>]+href="\/somewhere"/);
  });
});
