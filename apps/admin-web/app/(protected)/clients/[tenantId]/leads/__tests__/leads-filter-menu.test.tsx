import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import jwt from "jsonwebtoken";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

vi.mock("next/navigation", () => ({
  redirect: vi.fn((url: string) => {
    throw new Error(`REDIRECT:${url}`);
  }),
}));

const ClientLeadsPage = (await import("../page")).default;

const SECRET = process.env.JWT_SECRET as string;

function platformToken(): string {
  return jwt.sign({ sub: "platform-1", role: "PLATFORM_ADMIN", tenant_id: null, project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

describe("ClientLeadsPage SR-25 assignment menu", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("uses the tenant-scoped member list and renders only active target-tenant agents", async () => {
    getMock.mockReturnValue({ value: platformToken() });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/admin/tenants/tenant-1/users")) {
        return response([
          {
            id: "agent-1",
            tenant_id: "tenant-1",
            email: "active@example.test",
            role: "CLIENT_AGENT",
            name: "Active Agent",
            active: true,
            last_login_at: null,
          },
          {
            id: "agent-2",
            tenant_id: "tenant-1",
            email: "inactive@example.test",
            role: "CLIENT_AGENT",
            name: "Inactive Agent",
            active: false,
            last_login_at: null,
          },
        ]);
      }
      return response({
        items: [
          {
            lead_id: "lead-1",
            name: "Ada Lovelace",
            email: "ada@example.test",
            phone: null,
            status: "new",
            stage: "captured",
            qualification_score: null,
            assigned_agent_id: null,
            source: "widget",
            created_at: "2026-08-01T00:00:00Z",
          },
        ],
        total: 1,
        limit: 25,
        offset: 0,
      });
    });

    const element = await ClientLeadsPage({
      params: Promise.resolve({ tenantId: "tenant-1" }),
      searchParams: Promise.resolve({}),
    });
    const markup = renderToStaticMarkup(element);

    expect(markup).not.toContain("Assigned to me");
    expect(markup).toContain("Active Agent");
    expect(markup).not.toContain("Inactive Agent");
    expect(fetchSpy.mock.calls.map(([url]) => String(url)).join("\n")).toContain(
      "/admin/tenants/tenant-1/users"
    );
  });
});
