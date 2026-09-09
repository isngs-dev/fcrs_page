/**
 * Tests for the platform-admin "Clients" list grouped by account
 * (multi-chatbot accounts, Slice 6). Covers the pure `groupClientsByAccount`
 * bucketing logic directly (same extraction convention as `admin-shell.tsx`'s
 * `visibleGroupsForRole`), plus a structural render test mirroring
 * `clients/new/__tests__/page.test.tsx`'s real-JWT pattern + mocked
 * `adminApiFetch`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import jwt from "jsonwebtoken";
import { groupClientsByAccount } from "@/app/(protected)/clients/page";
import type { ClientSummary, ClientAccountSummary } from "@/lib/clients";

function tenant(overrides: Partial<ClientSummary> = {}): ClientSummary {
  return {
    tenantId: "tenant-1",
    name: "Tenant One",
    slug: "tenant-one",
    enabled: true,
    clientAccountId: "account-1",
    ...overrides,
  };
}

describe("groupClientsByAccount (pure)", () => {
  it("buckets multiple tenants under the same account into one group", () => {
    const groups = groupClientsByAccount(
      [
        tenant({ tenantId: "t1", clientAccountId: "acc-a" }),
        tenant({ tenantId: "t2", clientAccountId: "acc-a" }),
        tenant({ tenantId: "t3", clientAccountId: "acc-b" }),
      ],
      [
        { id: "acc-a", name: "Acme Roofing" },
        { id: "acc-b", name: "Beta Solar" },
      ]
    );

    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ accountId: "acc-a", accountName: "Acme Roofing" });
    expect(groups[0].tenants.map((t) => t.tenantId)).toEqual(["t1", "t2"]);
    expect(groups[1]).toMatchObject({ accountId: "acc-b", accountName: "Beta Solar" });
    expect(groups[1].tenants.map((t) => t.tenantId)).toEqual(["t3"]);
  });

  it("regression: renders identically-shaped groups when every account has exactly one chatbot (today's common case)", () => {
    const groups = groupClientsByAccount(
      [
        tenant({ tenantId: "t1", name: "Acme", clientAccountId: "acc-a" }),
        tenant({ tenantId: "t2", name: "Beta", clientAccountId: "acc-b" }),
        tenant({ tenantId: "t3", name: "Gamma", clientAccountId: "acc-c" }),
      ],
      [
        { id: "acc-a", name: "Acme" },
        { id: "acc-b", name: "Beta" },
        { id: "acc-c", name: "Gamma" },
      ]
    );

    expect(groups).toHaveLength(3);
    for (const group of groups) {
      expect(group.tenants).toHaveLength(1);
    }
    // No tenant is dropped or duplicated across groups.
    const allTenantIds = groups.flatMap((g) => g.tenants.map((t) => t.tenantId));
    expect(allTenantIds).toEqual(["t1", "t2", "t3"]);
  });

  it("falls back to the tenant's own name when the account name lookup misses (never a blank header)", () => {
    const groups = groupClientsByAccount(
      [tenant({ tenantId: "t1", name: "Orphan Bot", clientAccountId: "acc-missing" })],
      [] as ClientAccountSummary[]
    );

    expect(groups[0].accountName).toBe("Orphan Bot");
  });

  it("preserves first-seen account order, not the accounts list's order", () => {
    const groups = groupClientsByAccount(
      [
        tenant({ tenantId: "t1", clientAccountId: "acc-b" }),
        tenant({ tenantId: "t2", clientAccountId: "acc-a" }),
      ],
      [
        { id: "acc-a", name: "Acme" },
        { id: "acc-b", name: "Beta" },
      ]
    );

    expect(groups.map((g) => g.accountId)).toEqual(["acc-b", "acc-a"]);
  });
});

const getMock = vi.fn();
const adminApiFetchMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (path: string, init?: RequestInit) => adminApiFetchMock(path, init),
  };
});

const ClientsPage = (await import("@/app/(protected)/clients/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: null, project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

describe("ClientsPage (/clients) -- rendering", () => {
  afterEach(() => {
    getMock.mockReset();
    adminApiFetchMock.mockReset();
  });

  it("renders one section per account with the account name and chatbot count", async () => {
    getMock.mockReturnValue({ value: signToken("PLATFORM_ADMIN") });
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path === "/debug/tenants") {
        return Promise.resolve(
          jsonResponse([
            { id: "t1", name: "Bot One", slug: "bot-one", enabled: true, client_account_id: "acc-a" },
            { id: "t2", name: "Bot Two", slug: "bot-two", enabled: true, client_account_id: "acc-a" },
          ])
        );
      }
      if (path === "/debug/client-accounts") {
        return Promise.resolve(jsonResponse([{ id: "acc-a", name: "Acme Roofing" }]));
      }
      return Promise.reject(new Error("unexpected path: " + path));
    });

    const element = await ClientsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toContain("Acme Roofing");
    expect(html).toContain("2 chatbots");
    expect(html).toContain("Bot One");
    expect(html).toContain("Bot Two");
    expect(html).toContain("1 client");
  });

  it("degrades gracefully (tenants still render) when the accounts fetch fails", async () => {
    getMock.mockReturnValue({ value: signToken("PLATFORM_ADMIN") });
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path === "/debug/tenants") {
        return Promise.resolve(
          jsonResponse([
            { id: "t1", name: "Bot One", slug: "bot-one", enabled: true, client_account_id: "acc-a" },
          ])
        );
      }
      return Promise.reject(new Error("accounts endpoint down"));
    });

    const element = await ClientsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toContain("Bot One");
  });
});
