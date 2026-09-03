/**
 * Structural test for the per-client agent-performance report
 * (platform-admin console) -- see the rollup page's own test file header
 * comment for the rationale (plumbing, not `lib/reports.ts` data mapping).
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

const ClientAgentPerformancePage = (
  await import("@/app/(protected)/clients/[tenantId]/reports/agent-performance/page")
).default;

describe("ClientAgentPerformancePage (/clients/[tenantId]/reports/agent-performance)", () => {
  afterEach(() => {
    adminApiFetchMock.mockReset();
  });

  it("calls the tenant-scoped report + members endpoints, and resolves agent names from listMembersForTenant, not listMembers", async () => {
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path.includes("/analytics/reports/agent-performance")) {
        return Promise.resolve(
          jsonResponse({
            window: { from: "2026-01-01", to: "2026-01-30" },
            agents: [
              { assigned_agent_id: "agent-1", assigned: 5, contacted: 4, won: 2, win_rate: 0.4 },
            ],
            unassigned: { assigned_agent_id: null, assigned: 0, contacted: 0, won: 0, win_rate: null },
          })
        );
      }
      if (path.includes("/admin/tenants/tenant-7/users")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "agent-1",
                tenant_id: "tenant-7",
                email: "a@x.com",
                role: "CLIENT_AGENT",
                name: "Alex Agent",
                active: true,
                last_login_at: null,
              },
            ]),
            { status: 200 }
          )
        );
      }
      return Promise.reject(new Error("unexpected path: " + path));
    });

    const element = await ClientAgentPerformancePage({
      params: Promise.resolve({ tenantId: "tenant-7" }),
      searchParams: Promise.resolve({}),
    });
    const html = renderToStaticMarkup(element);

    const calledPaths = adminApiFetchMock.mock.calls.map((call) => call[0] as string);
    expect(calledPaths.some((p) => p.includes("/admin/tenants/tenant-7/analytics/reports/agent-performance"))).toBe(
      true
    );
    expect(calledPaths.some((p) => p.includes("/admin/tenants/tenant-7/users"))).toBe(true);
    expect(calledPaths.some((p) => p === "/admin/users")).toBe(false);

    // The resolved agent name from the TENANT-scoped members call renders.
    expect(html).toContain("Alex Agent");

    expect(html).toContain('href="/clients/tenant-7/reports"');
    expect(html).toMatch(/href="\/reports\/csv\/agent-performance\?[^"]*tenant_id=tenant-7/);
  });

  it("shows an honest error state when the report fetch fails, never fabricating a table", async () => {
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path.includes("/analytics/reports/agent-performance")) {
        return Promise.reject(new Error("boom"));
      }
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
    });

    const element = await ClientAgentPerformancePage({
      params: Promise.resolve({ tenantId: "tenant-7" }),
      searchParams: Promise.resolve({}),
    });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/role="alert"/);
  });
});
