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
  useRouter: () => ({ push: vi.fn() }),
}));

const AccountsPage = (await import("@/app/(protected)/accounts/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign(
    { sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] },
    SECRET,
    { algorithm: "HS256", expiresIn: "1h" }
  );
}

function emptyListResponse() {
  return new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 });
}

describe("AccountsPage RBAC-aware rendering (D2, MANDATORY)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("CLIENT_AGENT does NOT see 'Add account' anywhere in the rendered markup", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/Add account/i);
  });

  it("CLIENT_ADMIN DOES see 'Add account'", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Add account/i);
  });

  it("NEITHER role sees any edit affordance -- there is no PATCH /admin/accounts (M3/D2)", async () => {
    for (const role of ["CLIENT_ADMIN", "CLIENT_AGENT"]) {
      getMock.mockReturnValue({ value: signToken(role) });
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(
          JSON.stringify({
            items: [{ account_id: "account-1", name: "Acme", domain: null, created_at: "2026-07-01T00:00:00Z" }],
            total: 1,
            limit: 25,
            offset: 0,
          }),
          { status: 200 }
        )
      );

      const element = await AccountsPage({ searchParams: Promise.resolve({}) });
      const html = renderToStaticMarkup(element);

      expect(html).not.toMatch(/Edit account/i);
      vi.restoreAllMocks();
    }
  });

  it("PLATFORM_ADMIN is redirected away", async () => {
    getMock.mockReturnValue({ value: signToken("PLATFORM_ADMIN") });

    await expect(AccountsPage({ searchParams: Promise.resolve({}) })).rejects.toThrow("REDIRECT:/");
  });

  it("an unauthenticated request is redirected to /login", async () => {
    getMock.mockReturnValue(undefined);

    await expect(AccountsPage({ searchParams: Promise.resolve({}) })).rejects.toThrow("REDIRECT:/login");
  });

  it("the label is always 'Accounts', never 'Companies' (D1/D-naming)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Accounts/);
    expect(html).not.toMatch(/Companies/i);
  });

  it("does not render an Industry, Contacts-count, Owner, or Status column (D5/G1-G4: no fabricated field)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [{ account_id: "account-1", name: "Acme", domain: "acme.example.com", created_at: "2026-07-01T00:00:00Z" }],
          total: 1,
          limit: 25,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/>Industry</i);
    expect(html).not.toMatch(/>Contacts</);
    expect(html).not.toMatch(/>Owner</);
    expect(html).not.toMatch(/>Status</);
  });

  it("renders exactly 2 column headers -- Account and Created, no checkbox column (D5/D6/G5-G7)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [{ account_id: "account-1", name: "Acme", domain: "acme.example.com", created_at: "2026-07-01T00:00:00Z" }],
          total: 1,
          limit: 25,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    const headerMatches = html.match(/<th\b/g) ?? [];
    expect(headerMatches).toHaveLength(2);
    expect(html).not.toMatch(/type="checkbox"/);
  });

  it("renders no sort icons and no Filter button/funnels (D5/G5/G6: no backend sort/filter params)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [{ account_id: "account-1", name: "Acme", domain: "acme.example.com", created_at: "2026-07-01T00:00:00Z" }],
          total: 1,
          limit: 25,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/title="Sort"/);
    expect(html).not.toMatch(/title="Filter"/);
    expect(html).not.toMatch(/>Filter</);
  });

  it("renders the 28px/600 title", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("text-[28px]");
    expect(html).toContain("font-semibold");
  });

  it("renders a 30x30 mono-initial tile with the account name/domain stack", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [{ account_id: "account-1", name: "Acme", domain: "acme.example.com", created_at: "2026-07-01T00:00:00Z" }],
          total: 1,
          limit: 25,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("size-[30px]");
    expect(html).toMatch(/>AC</); // initials from "Acme"
    expect(html).toContain("acme.example.com");
  });

  it("an empty accounts list renders an explicit empty state, never placeholder rows", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/No accounts yet/i);
  });

  it("a failing list fetch renders the error treatment with its correlation ID", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "SOMETHING", message: "boom", correlation_id: "corr-88" }), {
        status: 500,
      })
    );

    const element = await AccountsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/corr-88/);
  });
});
