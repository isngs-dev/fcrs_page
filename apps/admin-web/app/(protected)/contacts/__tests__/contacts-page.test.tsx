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

const ContactsPage = (await import("@/app/(protected)/contacts/page")).default;

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

/**
 * SR-27 slice 5: `ContactsPage` now issues THREE parallel fetches
 * (`listContacts`, `listAccounts`, `listMembers`) to build the Company/Owner
 * name maps. Route by URL so each test can keep asserting against the
 * contacts list response while the other two calls get harmless empty
 * responses -- mirrors how `lib/*.ts` shapes its list endpoints.
 */
function mockThreeListFetch(contactsResponse: Response) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (url.includes("/admin/contacts")) return contactsResponse;
    if (url.includes("/admin/accounts")) {
      return new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 });
    }
    if (url.includes("/admin/users")) {
      return new Response(JSON.stringify({ items: [] }), { status: 200 });
    }
    return emptyListResponse();
  });
}

describe("ContactsPage RBAC-aware rendering (D2, MANDATORY)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("CLIENT_AGENT does NOT see 'Add contact' anywhere in the rendered markup", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT") });
    mockThreeListFetch(emptyListResponse());

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/Add contact/i);
  });

  it("CLIENT_ADMIN DOES see 'Add contact'", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockThreeListFetch(emptyListResponse());

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Add contact/i);
  });

  it("both CLIENT_ADMIN and CLIENT_AGENT reach the page (read is allowed to both, M2)", async () => {
    for (const role of ["CLIENT_ADMIN", "CLIENT_AGENT"]) {
      getMock.mockReturnValue({ value: signToken(role) });
      mockThreeListFetch(emptyListResponse());

      const element = await ContactsPage({ searchParams: Promise.resolve({}) });
      expect(redirectMock).not.toHaveBeenCalled();
      expect(element).toBeTruthy();
      vi.restoreAllMocks();
    }
  });

  it("PLATFORM_ADMIN is redirected away (not in this page's role allowlist, per SR-15 D6)", async () => {
    getMock.mockReturnValue({ value: signToken("PLATFORM_ADMIN") });

    await expect(ContactsPage({ searchParams: Promise.resolve({}) })).rejects.toThrow("REDIRECT:/");
  });

  it("an unauthenticated request is redirected to /login, not served a shell", async () => {
    getMock.mockReturnValue(undefined);

    await expect(ContactsPage({ searchParams: Promise.resolve({}) })).rejects.toThrow("REDIRECT:/login");
  });

  it("the page never renders the mockup's Leads/Contacts/Companies tab strip (D1)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockThreeListFetch(emptyListResponse());

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/Companies/);
  });

  it("the label is always 'Contacts', matching D-naming (never fabricated 'People' etc.)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockThreeListFetch(emptyListResponse());

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Contacts/);
  });

  it("renders the 28px/600 title", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockThreeListFetch(emptyListResponse());

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("text-[28px]");
    expect(html).toContain("font-semibold");
  });

  it("an empty contacts list renders an explicit empty state, never placeholder rows", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockThreeListFetch(emptyListResponse());

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/No contacts yet/i);
  });

  it("a failing list fetch renders the error treatment with its correlation ID", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockThreeListFetch(
      new Response(JSON.stringify({ error_code: "SOMETHING", message: "boom", correlation_id: "corr-77" }), {
        status: 500,
      })
    );

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/corr-77/);
  });

  it("renders a real Company header now that the column resolves account names (G8) -- and no Status/checkbox", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockThreeListFetch(
      new Response(
        JSON.stringify({
          items: [
            {
              contact_id: "contact-1",
              account_id: "account-1",
              lead_id: null,
              name: "Ada Lovelace",
              email: "ada@example.com",
              phone: null,
              owner_agent_id: null,
              created_at: "2026-07-15T00:00:00Z",
            },
          ],
          total: 1,
          limit: 25,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/>Company</);
    expect(html).not.toMatch(/>Status</);
    expect(html).not.toMatch(/type="checkbox"/);
  });

  it("resolves the real account name into the Company column when listAccounts returns a match (G8)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/admin/contacts")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                contact_id: "contact-1",
                account_id: "account-1",
                lead_id: null,
                name: "Ada Lovelace",
                email: "ada@example.com",
                phone: null,
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
      if (url.includes("/admin/accounts")) {
        return new Response(
          JSON.stringify({
            items: [{ account_id: "account-1", name: "Acme Corp", domain: null, created_at: "2026-07-01T00:00:00Z" }],
            total: 1,
            limit: 25,
            offset: 0,
          }),
          { status: 200 }
        );
      }
      if (url.includes("/admin/users")) {
        return new Response(JSON.stringify({ items: [] }), { status: 200 });
      }
      return emptyListResponse();
    });

    const element = await ContactsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Acme Corp/);
  });
});
