import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const { buildDealsQuery, listDeals, listAllDealsForBoard, getDealDetail, getDealsConfig, DEALS_PAGE_SIZE } =
  await import("@/lib/deals");

describe("DEALS_PAGE_SIZE", () => {
  it("is well within the backend's [1,200] clamp", () => {
    expect(DEALS_PAGE_SIZE).toBeGreaterThanOrEqual(1);
    expect(DEALS_PAGE_SIZE).toBeLessThanOrEqual(200);
  });
});

describe("buildDealsQuery", () => {
  it("page=1 -> limit=DEALS_PAGE_SIZE&offset=0", () => {
    const params = new URLSearchParams(buildDealsQuery({ page: 1 }));
    expect(params.get("limit")).toBe(String(DEALS_PAGE_SIZE));
    expect(params.get("offset")).toBe("0");
  });

  it("page=3 -> offset advances by DEALS_PAGE_SIZE", () => {
    const params = new URLSearchParams(buildDealsQuery({ page: 3 }));
    expect(params.get("offset")).toBe(String(DEALS_PAGE_SIZE * 2));
  });

  it("page=0 or negative -> clamped to offset=0", () => {
    expect(new URLSearchParams(buildDealsQuery({ page: 0 })).get("offset")).toBe("0");
    expect(new URLSearchParams(buildDealsQuery({ page: -3 })).get("offset")).toBe("0");
  });

  it("passes stage/ownerAgentId through when present", () => {
    const params = new URLSearchParams(buildDealsQuery({ page: 1, stage: "proposal", ownerAgentId: "agent-1" }));
    expect(params.get("stage")).toBe("proposal");
    expect(params.get("owner_agent_id")).toBe("agent-1");
  });

  it("omits stage/ownerAgentId when blank", () => {
    const params = new URLSearchParams(buildDealsQuery({ page: 1, stage: "  ", ownerAgentId: "" }));
    expect(params.has("stage")).toBe(false);
    expect(params.has("owner_agent_id")).toBe(false);
  });

  it("never carries a tenant_id (SR-17 D7)", () => {
    expect(buildDealsQuery({ page: 1 })).not.toMatch(/tenant/i);
  });
});

function dealBody(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    opportunity_id: "opp-1",
    contact_id: "contact-1",
    account_id: "account-1",
    name: "Acme renewal",
    amount: 1500.5,
    currency: "USD",
    stage: "prospecting",
    win_probability: 10,
    expected_close_date: "2026-09-01",
    closed_at: null,
    close_reason: null,
    owner_agent_id: "agent-1",
    created_at: "2026-07-15T00:00:00Z",
    ...overrides,
  };
}

describe("listDeals -- D6 money honesty at the JSON boundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("converts a JSON-numeric amount to a string immediately (never left as a JS number)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [dealBody({ amount: 1500.5 })], total: 1, limit: 25, offset: 0 }), {
        status: 200,
      })
    );

    const result = await listDeals({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(typeof result.items[0].amount).toBe("string");
      expect(result.items[0].amount).toBe("1500.5");
    }
  });

  it("preserves amount:null as null -- NEVER coerced to '0'/'0.00' (D6)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [dealBody({ amount: null })], total: 1, limit: 25, offset: 0 }), {
        status: 200,
      })
    );

    const result = await listDeals({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items[0].amount).toBeNull();
      expect(result.items[0].amount).not.toBe("0");
      expect(result.items[0].amount).not.toBe("0.00");
    }
  });

  it("carries the row's own currency verbatim, not a global assumption", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [dealBody({ opportunity_id: "opp-eur", currency: "EUR" }), dealBody({ opportunity_id: "opp-usd", currency: "USD" })],
          total: 2,
          limit: 25,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const result = await listDeals({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items[0].currency).toBe("EUR");
      expect(result.items[1].currency).toBe("USD");
    }
  });

  it("carries winProbability as a plain derived number, mapped snake->camel", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [dealBody({ win_probability: 42 })], total: 1, limit: 25, offset: 0 }), {
        status: 200,
      })
    );

    const result = await listDeals({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") expect(result.items[0].winProbability).toBe(42);
  });

  it("maps a full envelope field-by-field, snake->camel, no tenant_id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [dealBody()], total: 1, limit: 25, offset: 0 }), { status: 200 })
    );

    const result = await listDeals({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items[0]).toEqual({
        opportunityId: "opp-1",
        contactId: "contact-1",
        accountId: "account-1",
        name: "Acme renewal",
        amount: "1500.5",
        currency: "USD",
        stage: "prospecting",
        winProbability: 10,
        expectedCloseDate: "2026-09-01",
        closedAt: null,
        closeReason: null,
        ownerAgentId: "agent-1",
        createdAt: "2026-07-15T00:00:00Z",
      });
      expect(result.items[0]).not.toHaveProperty("tenant_id");
    }
  });

  it("targets the implicit /admin/opportunities path (D7: never tenant-explicit)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 }));

    await listDeals({ page: 1 });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toMatch(/^http:\/\/localhost:8000\/admin\/opportunities\?/);
    expect(url).not.toMatch(/\/admin\/tenants\//);
  });

  it("maps a 403 to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "c1" }), {
        status: 403,
      })
    );

    const result = await listDeals({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
      expect(result.correlationId).toBe("c1");
    }
  });

  it("maps a network throw to a generic message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await listDeals({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/unable to reach/i);
  });
});

describe("listAllDealsForBoard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("requests limit=200&offset=0 unfiltered (D2's whole-pipeline-visible board)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0, limit: 200, offset: 0 }), { status: 200 }));

    await listAllDealsForBoard();

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/opportunities?limit=200&offset=0");
  });

  it("also preserves null amounts as null", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [dealBody({ amount: null })], total: 1, limit: 200, offset: 0 }), {
        status: 200,
      })
    );

    const result = await listAllDealsForBoard();
    expect(result.status).toBe("ok");
    if (result.status === "ok") expect(result.items[0].amount).toBeNull();
  });
});

describe("getDealDetail", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 to an ok result", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(dealBody()), { status: 200 }));

    const result = await getDealDetail("opp-1");
    expect(result.status).toBe("ok");
    if (result.status === "ok") expect(result.deal.opportunityId).toBe("opp-1");
  });

  it("maps a 404 to a not-found message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "NOT_FOUND", message: "x", correlation_id: "c" }), { status: 404 })
    );

    const result = await getDealDetail("opp-1");
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/not be found/i);
  });
});

describe("getDealsConfig", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps currency + stage_probabilities -> stageProbabilities", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ currency: "GBP", stage_probabilities: { prospecting: 5, qualification: 20 } }),
        { status: 200 }
      )
    );

    const result = await getDealsConfig();
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.config.currency).toBe("GBP");
      expect(result.config.stageProbabilities).toEqual({ prospecting: 5, qualification: 20 });
    }
  });

  it("targets GET /admin/opportunities/config -- never the config PUT", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ currency: "USD", stage_probabilities: {} }), { status: 200 })
    );

    await getDealsConfig();

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit | undefined];
    expect(url).toBe("http://localhost:8000/admin/opportunities/config");
    expect(init?.method ?? "GET").not.toBe("PUT");
  });
});
