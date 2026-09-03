import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const usePathnameMock = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => usePathnameMock(),
}));

const { ClientConsoleTabs } = await import(
  "@/app/(protected)/clients/[tenantId]/client-console-tabs"
);

describe("ClientConsoleTabs", () => {
  it("renders exactly Analytics/Reports/Knowledge/Leads, tenant-prefixed, and never a Settings tab", () => {
    usePathnameMock.mockReturnValue("/clients/tenant-1/analytics");

    const html = renderToStaticMarkup(<ClientConsoleTabs tenantId="tenant-1" />);

    expect(html).toContain('href="/clients/tenant-1/analytics"');
    expect(html).toContain('href="/clients/tenant-1/reports"');
    expect(html).toContain('href="/clients/tenant-1/knowledge"');
    expect(html).toContain('href="/clients/tenant-1/leads"');
    expect(html).not.toMatch(/Settings/i);
  });

  it("marks the current section active via aria-current, and only that one", () => {
    usePathnameMock.mockReturnValue("/clients/tenant-1/reports/funnel");

    const html = renderToStaticMarkup(<ClientConsoleTabs tenantId="tenant-1" />);

    expect(html.match(/aria-current="page"/g) ?? []).toHaveLength(1);
    const reportsLinkMatch = html.match(/<a[^>]*href="\/clients\/tenant-1\/reports"[^>]*>/);
    expect(reportsLinkMatch?.[0]).toContain('aria-current="page"');
  });

  it("marks nothing active when the pathname matches no tab (e.g. a future top-level route)", () => {
    usePathnameMock.mockReturnValue("/clients/tenant-1/some-other-thing");

    const html = renderToStaticMarkup(<ClientConsoleTabs tenantId="tenant-1" />);

    expect(html).not.toContain('aria-current="page"');
  });
});
