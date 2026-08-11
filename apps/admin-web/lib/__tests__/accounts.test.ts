import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const {
  buildAccountsQuery,
  listAccounts,
  getAccountDetail,
  ACCOUNTS_PAGE_SIZE,
  ACCOUNTS_SERVER_LIMIT_MIN,
  ACCOUNTS_SERVER_LIMIT_MAX,
} = await import("@/lib/accounts");

describe("ACCOUNTS_PAGE_SIZE / server clamp bounds", () => {
  it("the page size is within the backend's documented [1,200] clamp (M3)", () => {
    expect(ACCOUNTS_SERVER_LIMIT_MIN).toBe(1);
    expect(ACCOUNTS_SERVER_LIMIT_MAX).toBe(200);
    expect(ACCOUNTS_PAGE_SIZE).toBeGreaterThanOrEqual(ACCOUNTS_SERVER_LIMIT_MIN);
    expect(ACCOUNTS_PAGE_SIZE).toBeLessThanOrEqual(ACCOUNTS_SERVER_LIMIT_MAX);
  });
});

describe("buildAccountsQuery", () => {
  it("page=1 -> limit=ACCOUNTS_PAGE_SIZE&offset=0", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 1 }));
    expect(params.get("limit")).toBe(String(ACCOUNTS_PAGE_SIZE));
    expect(params.get("offset")).toBe("0");
  });

  it("page=2 -> offset advances by ACCOUNTS_PAGE_SIZE", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 2 }));
    expect(params.get("offset")).toBe(String(ACCOUNTS_PAGE_SIZE));
  });

  it("page=0 or negative -> clamped to offset=0", () => {
    expect(new URLSearchParams(buildAccountsQuery({ page: 0 })).get("offset")).toBe("0");
    expect(new URLSearchParams(buildAccountsQuery({ page: -1 })).get("offset")).toBe("0");
  });

  it("does NOT re-implement the server's [1,200] clamp -- always sends the fixed page size verbatim, letting the server be the sole enforcement point (D6/M3)", () => {
    // A caller cannot even construct an out-of-range request through this
    // function's public params -- `limit` isn't caller-settable at all,
    // which is the point: no client-side "correction" of a different
    // meaning is possible.
    const params = new URLSearchParams(buildAccountsQuery({ page: 1 }));
    expect(Number(params.get("limit"))).toBeLessThanOrEqual(ACCOUNTS_SERVER_LIMIT_MAX);
  });

  it("never carries a tenant_id (D7)", () => {
    expect(buildAccountsQuery({ page: 1 })).not.toMatch(/tenant/i);
  });

  it("includes sort and dir when sort is a valid key (SR-29)", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 1, sort: "name", direction: "asc" }));
    expect(params.get("sort")).toBe("name");
    expect(params.get("dir")).toBe("asc");
  });

  it("omits sort when the key is not in the allowlist (SR-29)", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 1, sort: "bogus" }));
    expect(params.has("sort")).toBe(false);
    expect(params.has("dir")).toBe(false);
  });

  it("omits dir without a valid sort (SR-29)", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 1, direction: "asc" }));
    expect(params.has("dir")).toBe(false);
  });

  it("falls back to the per-key default direction when dir is invalid (SR-29)", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 1, sort: "created", direction: "sideways" }));
    expect(params.get("dir")).toBe("desc");
  });

  it("includes q when at least 2 characters (SR-29)", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 1, q: "ac" }));
    expect(params.get("q")).toBe("ac");
  });

  it("omits q when blank or a single character (SR-29)", () => {
    expect(new URLSearchParams(buildAccountsQuery({ page: 1, q: "" })).has("q")).toBe(false);
    expect(new URLSearchParams(buildAccountsQuery({ page: 1, q: "a" })).has("q")).toBe(false);
  });

  it("preserves existing limit/offset behavior when sort/q are absent (regression)", () => {
    const params = new URLSearchParams(buildAccountsQuery({ page: 3 }));
    expect(params.get("limit")).toBe(String(ACCOUNTS_PAGE_SIZE));
    expect(params.get("offset")).toBe(String(ACCOUNTS_PAGE_SIZE * 2));
    expect(params.has("sort")).toBe(false);
    expect(params.has("q")).toBe(false);
  });
});

describe("listAccounts", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope field-by-field, snake->camel, no tenant_id, no industry, no contact count (D5)", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const body = {
      items: [
        {
          account_id: "account-1",
          name: "Acme Corp",
          domain: "acme.example.com",
          created_at: "2026-07-01T00:00:00Z",
        },
      ],
      total: 4,
      limit: 25,
      offset: 0,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));

    const result = await listAccounts({ page: 1 });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.total).toBe(4);
      expect(result.items[0]).toEqual({
        accountId: "account-1",
        name: "Acme Corp",
        domain: "acme.example.com",
        createdAt: "2026-07-01T00:00:00Z",
      });
      expect(result.items[0]).not.toHaveProperty("tenant_id");
      expect(result.items[0]).not.toHaveProperty("industry");
      expect(result.items[0]).not.toHaveProperty("contactCount");
      expect(result.items[0]).not.toHaveProperty("contact_count");
    }
  });

  it("preserves a null domain rather than coercing", async () => {
    getMock.mockReturnValue(undefined);
    const body = {
      items: [{ account_id: "account-2", name: "No Domain LLC", domain: null, created_at: "2026-07-01T00:00:00Z" }],
      total: 1,
      limit: 25,
      offset: 0,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));

    const result = await listAccounts({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") expect(result.items[0].domain).toBeNull();
  });

  it("targets the implicit /admin/accounts path (D7)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 }));

    await listAccounts({ page: 1 });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe(`http://localhost:8000/admin/accounts?limit=${ACCOUNTS_PAGE_SIZE}&offset=0`);
    expect(url).not.toMatch(/tenant/i);
  });

  it("issues exactly ONE fetch for a 50-row list render (D5 no-N+1)", async () => {
    getMock.mockReturnValue(undefined);
    const items = Array.from({ length: 50 }, (_, i) => ({
      account_id: `account-${i}`,
      name: `Account ${i}`,
      domain: null,
      created_at: "2026-07-01T00:00:00Z",
    }));
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items, total: 50, limit: 50, offset: 0 }), { status: 200 }));

    const result = await listAccounts({ page: 1 });

    expect(result.status).toBe("ok");
    if (result.status === "ok") expect(result.items).toHaveLength(50);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "corr-1" }), {
        status: 403,
      })
    );

    const result = await listAccounts({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/permission/i);
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await listAccounts({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/unable to reach/i);
  });

  it("maps INVALID_ACCOUNT_SORT to a friendly filter message (SR-29)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "INVALID_ACCOUNT_SORT", message: "x", correlation_id: "c" }), {
        status: 422,
      })
    );

    const result = await listAccounts({ page: 1, sort: "name" });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/filter isn't valid/i);
  });

  it("maps INVALID_ACCOUNT_SEARCH to a friendly filter message (SR-29)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "INVALID_ACCOUNT_SEARCH", message: "x", correlation_id: "c" }), {
        status: 422,
      })
    );

    const result = await listAccounts({ page: 1, q: "ac" });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/filter isn't valid/i);
  });
});

describe("getAccountDetail", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope to an ok result with no tenant_id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ account_id: "account-1", name: "Acme Corp", domain: null, created_at: "2026-07-01T00:00:00Z" }),
        { status: 200 }
      )
    );

    const result = await getAccountDetail("account-1");
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.account.accountId).toBe("account-1");
      expect(result.account).not.toHaveProperty("tenant_id");
    }
  });

  it("maps a 404 to a not-found message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "NOT_FOUND", message: "x", correlation_id: "c" }), { status: 404 })
    );

    const result = await getAccountDetail("account-1");
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/not be found/i);
  });
});
