/**
 * Structural test for the per-client ROI dashboard (platform-admin console)
 * -- see the agent-performance per-client page test's header comment for the
 * rationale (plumbing, not `lib/reports.ts` data mapping).
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

function jsonResponse(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

const ClientRoiDashboardPage = (
  await import("@/app/(protected)/clients/[tenantId]/reports/roi/page")
).default;

describe("ClientRoiDashboardPage (/clients/[tenantId]/reports/roi)", () => {
  afterEach(() => {
    adminApiFetchMock.mockReset();
  });

  it("calls leads-over-time, bookings, and lead-sources against the tenant-scoped path, never the implicit one", async () => {
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path.includes("/analytics/reports/leads-over-time")) {
        return Promise.resolve(
          jsonResponse({
            window: { from: "2026-07-01", to: "2026-08-01", bucket: "week" },
            series: [{ bucket_start: "2026-07-06", count: 3 }],
            totals: 3,
          })
        );
      }
      if (path.includes("/analytics/reports/bookings")) {
        return Promise.resolve(
          jsonResponse({
            window: { from: "2026-07-01", to: "2026-08-01", bucket: "week" },
            series: [
              {
                bucket_start: "2026-07-06",
                booked: 2,
                completed: 0,
                no_show: 0,
                cancelled: 0,
                total_excluding_cancelled: 2,
              },
            ],
            totals: { booked: 2, completed: 0, no_show: 0, cancelled: 0, total_excluding_cancelled: 2 },
          })
        );
      }
      if (path.includes("/analytics/reports/lead-sources")) {
        return Promise.resolve(
          jsonResponse({
            window: { from: "2026-07-01", to: "2026-08-01" },
            sources: [{ source: "widget", count: 3, percentage: 100 }],
            total: 3,
            single_source: true,
          })
        );
      }
      return Promise.reject(new Error("unexpected path: " + path));
    });

    const element = await ClientRoiDashboardPage({
      params: Promise.resolve({ tenantId: "tenant-7" }),
      searchParams: Promise.resolve({}),
    });
    const html = renderToStaticMarkup(element);

    const calledPaths = adminApiFetchMock.mock.calls.map((call) => call[0] as string);
    for (const report of ["leads-over-time", "bookings", "lead-sources"]) {
      expect(calledPaths.some((p) => p.includes(`/admin/tenants/tenant-7/analytics/reports/${report}`))).toBe(true);
      expect(calledPaths.some((p) => p === `/admin/analytics/reports/${report}`)).toBe(false);
    }

    expect(html).toContain('href="/clients/tenant-7/reports"');
    expect(html).toMatch(/href="\/reports\/csv\/leads-over-time\?[^"]*tenant_id=tenant-7/);
  });

  it("shows an honest error state when the leads-over-time fetch fails, never fabricating the chart", async () => {
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path.includes("/analytics/reports/leads-over-time")) {
        return Promise.reject(new Error("boom"));
      }
      return Promise.resolve(
        jsonResponse({
          window: { from: "2026-07-01", to: "2026-08-01" },
          sources: [],
          total: 0,
          single_source: false,
        })
      );
    });

    const element = await ClientRoiDashboardPage({
      params: Promise.resolve({ tenantId: "tenant-7" }),
      searchParams: Promise.resolve({}),
    });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/role="alert"/);
  });
});
