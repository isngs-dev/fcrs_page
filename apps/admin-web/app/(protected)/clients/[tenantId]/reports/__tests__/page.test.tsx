/**
 * Structural test for the per-client reports rollup (platform-admin
 * console). Every underlying report fetch is made to fail deliberately --
 * the point here isn't re-testing `lib/reports.ts`'s own data mapping
 * (already covered where that lives), it's proving the PLUMBING is correct:
 * every fetcher is called against the tenant-scoped backend path, agent
 * names come from `listMembersForTenant` (not `listMembers`), and every
 * deep-link/CSV href is prefixed with `/clients/{tenantId}/...` -- exactly
 * the two things this page adds over its client-facing sibling.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const adminApiFetchMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (path: string, init?: RequestInit) => adminApiFetchMock(path, init),
  };
});

const ClientReportsIndexPage = (
  await import("@/app/(protected)/clients/[tenantId]/reports/page")
).default;

describe("ClientReportsIndexPage (/clients/[tenantId]/reports)", () => {
  afterEach(() => {
    adminApiFetchMock.mockReset();
  });

  it("calls every report fetcher against the tenant-scoped path, resolves agents via listMembersForTenant, and prefixes every deep link/CSV href with /clients/{tenantId}/reports", async () => {
    adminApiFetchMock.mockImplementation(() => Promise.reject(new Error("boom")));

    const element = await ClientReportsIndexPage({
      params: Promise.resolve({ tenantId: "tenant-42" }),
      searchParams: Promise.resolve({}),
    });
    const html = renderToStaticMarkup(element);

    const calledPaths = adminApiFetchMock.mock.calls.map((call) => call[0] as string);

    for (const report of [
      "leads-by-stage",
      "win-loss",
      "funnel",
      "lead-sources",
      "score-distribution",
      "agent-performance",
      "recent-conversions",
    ]) {
      expect(calledPaths.some((p) => p.includes(`/admin/tenants/tenant-42/analytics/reports/${report}`))).toBe(
        true
      );
      // Never the implicit (own-tenant) path for the same report.
      expect(calledPaths.some((p) => p === `/admin/analytics/reports/${report}`)).toBe(false);
    }

    // Agent names resolved via the tenant-scoped members endpoint.
    expect(calledPaths.some((p) => p.includes("/admin/tenants/tenant-42/users"))).toBe(true);
    expect(calledPaths.some((p) => p === "/admin/users")).toBe(false);

    // Every "Full reports" deep link is tenant-prefixed.
    for (const report of [
      "leads-by-stage",
      "bookings",
      "funnel",
      "win-loss",
      "lead-sources",
      "score-distribution",
      "agent-performance",
      "recent-conversions",
    ]) {
      expect(html).toContain(`href="/clients/tenant-42/reports/${report}"`);
    }

    // The CSV download link is tenant-scoped too.
    expect(html).toMatch(/href="\/reports\/csv\/leads-by-stage\?[^"]*tenant_id=tenant-42/);

    // No "Back to console" link on this page -- the layout's own breadcrumb
    // + tab bar already cover that (decision documented in the page itself).
    expect(html).not.toMatch(/Back to console/);
  });
});
