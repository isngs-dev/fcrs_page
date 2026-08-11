import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import jwt from "jsonwebtoken";

const getMock = vi.fn();
const redirectMock = vi.fn((url: string) => {
  throw new Error(`REDIRECT:${url}`);
});

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

vi.mock("next/navigation", () => ({
  redirect: redirectMock,
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const DealsPage = (await import("@/app/(protected)/deals/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function emptyListResponse() {
  return new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 });
}

function dealListResponse() {
  return new Response(
    JSON.stringify({
      items: [
        {
          opportunity_id: "opp-1",
          contact_id: "contact-1",
          account_id: "account-1",
          name: "Acme renewal",
          amount: null,
          currency: "USD",
          stage: "prospecting",
          win_probability: 10,
          expected_close_date: null,
          closed_at: null,
          close_reason: null,
          owner_agent_id: null,
          created_at: "2026-07-15T00:00:00Z",
        },
      ],
      total: 1,
      limit: 25,
      offset: 0,
    }),
    { status: 200 }
  );
}

describe("DealsPage RBAC-aware rendering (SR-18 -- this sprint's own D-deals-page)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("both CLIENT_ADMIN and CLIENT_AGENT reach the page (M10: agents work deals in full)", async () => {
    for (const role of ["CLIENT_ADMIN", "CLIENT_AGENT"]) {
      getMock.mockReturnValue({ value: signToken(role) });
      vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

      const element = await DealsPage({ searchParams: Promise.resolve({}) });
      expect(redirectMock).not.toHaveBeenCalled();
      expect(element).toBeTruthy();
      vi.restoreAllMocks();
    }
  });

  it("PLATFORM_ADMIN is redirected away (not in this page's role allowlist, matching /leads and /contacts)", async () => {
    getMock.mockReturnValue({ value: signToken("PLATFORM_ADMIN") });
    await expect(DealsPage({ searchParams: Promise.resolve({}) })).rejects.toThrow("REDIRECT:/");
  });

  it("an unauthenticated request is redirected to /login, not served a shell", async () => {
    getMock.mockReturnValue(undefined);
    await expect(DealsPage({ searchParams: Promise.resolve({}) })).rejects.toThrow("REDIRECT:/login");
  });

  it("the placeholder copy ('This page arrives in the next release') is gone -- the real page replaced it", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await DealsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/arrives in the next release/i);
  });

  it("an empty deals list renders an explicit empty state, never placeholder rows", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await DealsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/No deals yet/i);
  });

  it("a failing list fetch renders the error treatment with its correlation ID", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "SOMETHING", message: "boom", correlation_id: "corr-99" }), {
        status: 500,
      })
    );

    const element = await DealsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/corr-99/);
  });

  it("a deal with a null amount renders 'Not quoted' on the Table view, never $0 (D6)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(dealListResponse());

    const element = await DealsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Not quoted/);
  });

  it("?view=board renders the real PipelineBoard, not a 'coming soon' panel (D1 -- Deals shipped WITH the board from day one)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              opportunity_id: "opp-1",
              contact_id: "contact-1",
              account_id: null,
              name: "Acme renewal",
              amount: null,
              currency: "USD",
              stage: "prospecting",
              win_probability: 10,
              expected_close_date: null,
              closed_at: null,
              close_reason: null,
              owner_agent_id: null,
              created_at: "2026-07-15T00:00:00Z",
            },
          ],
          total: 1,
          limit: 200,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const element = await DealsPage({ searchParams: Promise.resolve({ view: "board" }) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/coming soon/i);
    expect(html).toMatch(/Prospecting/);
    expect(html).toMatch(/Acme renewal/);
  });

  it("never sends a tenant_id anywhere in the outgoing fetch URL (SR-17 D7)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    await DealsPage({ searchParams: Promise.resolve({}) });

    for (const call of fetchSpy.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toMatch(/\/admin\/tenants\//);
      expect(url).not.toMatch(/tenant_id/);
    }
  });

  it("the 'Add deal' affordance is honestly disabled, never a broken/fabricated form (no create form built this sprint)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await DealsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Add deal/);
    expect(html).toMatch(/aria-disabled="true"[^>]*>\s*\+ Add deal|Add deal[\s\S]*aria-disabled/);
  });
});
