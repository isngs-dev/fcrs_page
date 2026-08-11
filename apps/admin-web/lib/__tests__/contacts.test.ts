import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const { buildContactsQuery, listContacts, getContactDetail, CONTACTS_PAGE_SIZE } = await import(
  "@/lib/contacts"
);

describe("CONTACTS_PAGE_SIZE", () => {
  it("is well within the backend's [1,200] clamp (M1)", () => {
    expect(CONTACTS_PAGE_SIZE).toBeGreaterThanOrEqual(1);
    expect(CONTACTS_PAGE_SIZE).toBeLessThanOrEqual(200);
  });
});

describe("buildContactsQuery", () => {
  it("page=1 -> limit=CONTACTS_PAGE_SIZE&offset=0", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 1 }));
    expect(params.get("limit")).toBe(String(CONTACTS_PAGE_SIZE));
    expect(params.get("offset")).toBe("0");
  });

  it("page=3 -> offset advances by CONTACTS_PAGE_SIZE", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 3 }));
    expect(params.get("offset")).toBe(String(CONTACTS_PAGE_SIZE * 2));
  });

  it("page=0 or negative -> clamped to offset=0 (page 1)", () => {
    expect(new URLSearchParams(buildContactsQuery({ page: 0 })).get("offset")).toBe("0");
    expect(new URLSearchParams(buildContactsQuery({ page: -5 })).get("offset")).toBe("0");
  });

  it("a non-finite page -> clamped to page 1", () => {
    expect(new URLSearchParams(buildContactsQuery({ page: NaN })).get("offset")).toBe("0");
  });

  it("never carries a tenant_id (D7)", () => {
    const qs = buildContactsQuery({ page: 1 });
    expect(qs).not.toMatch(/tenant/i);
  });

  it("includes sort and dir when sort is a valid key (SR-29)", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 1, sort: "email", direction: "asc" }));
    expect(params.get("sort")).toBe("email");
    expect(params.get("dir")).toBe("asc");
  });

  it("omits sort when the key is not in the allowlist (SR-29)", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 1, sort: "phone" }));
    expect(params.has("sort")).toBe(false);
  });

  it("omits dir without a valid sort (SR-29)", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 1, direction: "asc" }));
    expect(params.has("dir")).toBe(false);
  });

  it("includes account_id when present (SR-29)", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 1, accountId: "acc-1" }));
    expect(params.get("account_id")).toBe("acc-1");
  });

  it("omits account_id when blank (SR-29)", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 1, accountId: "" }));
    expect(params.has("account_id")).toBe(false);
  });

  it("includes q when at least 2 characters (SR-29)", () => {
    const params = new URLSearchParams(buildContactsQuery({ page: 1, q: "al" }));
    expect(params.get("q")).toBe("al");
  });

  it("omits q when blank or a single character (SR-29)", () => {
    expect(new URLSearchParams(buildContactsQuery({ page: 1, q: "" })).has("q")).toBe(false);
    expect(new URLSearchParams(buildContactsQuery({ page: 1, q: "a" })).has("q")).toBe(false);
  });
});

describe("listContacts", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope field-by-field, snake->camel, no tenant_id", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const body = {
      items: [
        {
          contact_id: "contact-1",
          account_id: "account-1",
          lead_id: "lead-1",
          name: "Ada Lovelace",
          email: "ada@example.com",
          phone: "+15551234567",
          owner_agent_id: "agent-1",
          created_at: "2026-07-15T00:00:00Z",
        },
      ],
      total: 12,
      limit: 25,
      offset: 0,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));

    const result = await listContacts({ page: 1 });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.total).toBe(12);
      expect(result.items).toHaveLength(1);
      expect(result.items[0]).toEqual({
        contactId: "contact-1",
        accountId: "account-1",
        leadId: "lead-1",
        name: "Ada Lovelace",
        email: "ada@example.com",
        phone: "+15551234567",
        ownerAgentId: "agent-1",
        createdAt: "2026-07-15T00:00:00Z",
      });
      expect(result.items[0]).not.toHaveProperty("tenant_id");
      expect(result.items[0]).not.toHaveProperty("tenantId");
    }
  });

  it("preserves null name/email/phone/account_id/owner_agent_id rather than coercing (no-silent-fallback)", async () => {
    getMock.mockReturnValue(undefined);
    const body = {
      items: [
        {
          contact_id: "contact-2",
          account_id: null,
          lead_id: null,
          name: null,
          email: null,
          phone: null,
          owner_agent_id: null,
          created_at: "2026-07-15T00:00:00Z",
        },
      ],
      total: 1,
      limit: 25,
      offset: 0,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));

    const result = await listContacts({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items[0].accountId).toBeNull();
      expect(result.items[0].name).toBeNull();
      expect(result.items[0].email).toBeNull();
    }
  });

  it("targets the implicit /admin/contacts path (D7: never a tenant-explicit route)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 }));

    await listContacts({ page: 1 });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe(`http://localhost:8000/admin/contacts?limit=${CONTACTS_PAGE_SIZE}&offset=0`);
    expect(url).not.toMatch(/tenant/i);
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "corr-1" }), {
        status: 403,
      })
    );

    const result = await listContacts({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
      expect(result.correlationId).toBe("corr-1");
    }
  });

  it("maps a 401 to a session-expired message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "AUTHENTICATION_ERROR", message: "x", correlation_id: "c" }), {
        status: 401,
      })
    );

    const result = await listContacts({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/session/i);
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await listContacts({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/unable to reach/i);
  });

  it("never logs the response body (PII-minimal)", async () => {
    getMock.mockReturnValue(undefined);
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              contact_id: "contact-1",
              account_id: null,
              lead_id: null,
              name: "Secret Name",
              email: "secret@example.com",
              phone: "+15551234567",
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

    await listContacts({ page: 1 });

    expect(consoleSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("issues exactly ONE fetch for a 50-row list render (D5 no-N+1)", async () => {
    getMock.mockReturnValue(undefined);
    const items = Array.from({ length: 50 }, (_, i) => ({
      contact_id: `contact-${i}`,
      account_id: `account-${i}`,
      lead_id: null,
      name: `Contact ${i}`,
      email: `contact${i}@example.com`,
      phone: null,
      owner_agent_id: null,
      created_at: "2026-07-15T00:00:00Z",
    }));
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items, total: 50, limit: 50, offset: 0 }), { status: 200 }));

    const result = await listContacts({ page: 1 });

    expect(result.status).toBe("ok");
    if (result.status === "ok") expect(result.items).toHaveLength(50);
    // The load-bearing assertion: mapping 50 rows client-side triggers no
    // additional network calls to resolve names/accounts (D5's forbidden
    // N+1) -- one list call in, 50 items out.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("maps INVALID_CONTACT_SORT to a friendly filter message (SR-29)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "INVALID_CONTACT_SORT", message: "x", correlation_id: "c" }), {
        status: 422,
      })
    );

    const result = await listContacts({ page: 1, sort: "name" });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/filter isn't valid/i);
  });

  it("maps INVALID_CONTACT_SEARCH to a friendly filter message (SR-29)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "INVALID_CONTACT_SEARCH", message: "x", correlation_id: "c" }), {
        status: 422,
      })
    );

    const result = await listContacts({ page: 1, q: "al" });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/filter isn't valid/i);
  });
});

describe("getContactDetail", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope to an ok result with no tenant_id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          contact_id: "contact-1",
          account_id: "account-1",
          lead_id: null,
          name: "Ada Lovelace",
          email: "ada@example.com",
          phone: null,
          owner_agent_id: null,
          created_at: "2026-07-15T00:00:00Z",
        }),
        { status: 200 }
      )
    );

    const result = await getContactDetail("contact-1");
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.contact.contactId).toBe("contact-1");
      expect(result.contact).not.toHaveProperty("tenant_id");
    }
  });

  it("maps a 404 to a not-found message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "NOT_FOUND", message: "x", correlation_id: "c" }), { status: 404 })
    );

    const result = await getContactDetail("contact-1");
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/not be found/i);
  });

  it("never targets a /admin/tenants/{id}/contacts route (D7)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          contact_id: "contact-1",
          account_id: null,
          lead_id: null,
          name: "Ada",
          email: "ada@example.com",
          phone: null,
          owner_agent_id: null,
          created_at: "2026-07-15T00:00:00Z",
        }),
        { status: 200 }
      )
    );

    await getContactDetail("contact-1");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/contacts/contact-1");
    expect(url).not.toMatch(/\/admin\/tenants\//);
  });
});
