/**
 * Structural test for the "My chatbots" hub (multi-chatbot accounts).
 * Mirrors `clients/new/__tests__/page.test.tsx`'s real-JWT-via-mocked-
 * `next/headers` pattern, plus mocking `@/lib/api`'s `adminApiFetch` for
 * the chatbots-list data (same pattern as
 * `clients/[tenantId]/knowledge/__tests__/page.test.tsx`).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import jwt from "jsonwebtoken";

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

const ChatbotsPage = (await import("@/app/(protected)/chatbots/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string, tenantId: string | null = "tenant-1"): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: tenantId, project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("ChatbotsPage (/chatbots)", () => {
  afterEach(() => {
    getMock.mockReset();
    adminApiFetchMock.mockReset();
  });

  it("renders one tile per chatbot, marks the active one, and shows 'Add a chatbot' for CLIENT_ADMIN", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN", "tenant-1") });
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({
        tenants: [
          { id: "tenant-1", name: "Bot One", slug: "bot-one", enabled: true },
          { id: "tenant-2", name: "Bot Two", slug: "bot-two", enabled: true },
        ],
      })
    );

    const element = await ChatbotsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("Bot One");
    expect(html).toContain("Bot Two");
    expect(html).toContain("2 chatbots on your account");
    expect(html).toContain("Currently active");
    expect(html).toMatch(/Switch to this chatbot/);
    expect(html).toContain("Add a chatbot");
    expect(adminApiFetchMock).toHaveBeenCalledWith("/admin/tenants/mine", undefined);
  });

  it("CLIENT_AGENT sees the same tiles but never the 'Add a chatbot' affordance", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT", "tenant-1") });
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ tenants: [{ id: "tenant-1", name: "Bot One", slug: "bot-one", enabled: true }] })
    );

    const element = await ChatbotsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("Bot One");
    expect(html).not.toContain("Add a chatbot");
    expect(html).not.toContain('href="/chatbots/new"');
  });

  it("shows an honest empty state instead of a fabricated tile", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN", "tenant-1") });
    adminApiFetchMock.mockResolvedValue(jsonResponse({ tenants: [] }));

    const element = await ChatbotsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/No chatbots yet/);
  });

  it("shows an honest error state on a backend failure, never a fabricated list", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN", "tenant-1") });
    adminApiFetchMock.mockRejectedValue(new Error("network down"));

    const element = await ChatbotsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/role="alert"/);
  });

  it("renders the switchError banner when redirected back with a failed switch", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN", "tenant-1") });
    adminApiFetchMock.mockResolvedValue(jsonResponse({ tenants: [] }));

    const element = await ChatbotsPage({
      searchParams: Promise.resolve({ switchError: "TENANT_NOT_FOUND" }),
    });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("switch chatbots");
    expect(html).toContain("TENANT_NOT_FOUND");
  });

  it("a disabled chatbot's switch button is disabled, not just cosmetically styled", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN", "tenant-1") });
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({
        tenants: [
          { id: "tenant-1", name: "Bot One", slug: "bot-one", enabled: true },
          { id: "tenant-2", name: "Bot Two", slug: "bot-two", enabled: false },
        ],
      })
    );

    const element = await ChatbotsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/<button[^>]*disabled[^>]*>\s*Switch to this chatbot/);
  });
});
