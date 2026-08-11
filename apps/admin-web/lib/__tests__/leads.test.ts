import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

// Imported after the mock is registered so the module under test picks up
// the mocked `next/headers` (adminApiFetch reads the access_token cookie).
const {
  buildLeadsQuery,
  listLeads,
  getLeadDetail,
  getLeadActivities,
  LEAD_STAGES,
  LEAD_STATUSES,
  stageBadgeStyle,
  scoreChipStyle,
  initialsFromName,
  LEAD_SORT_KEYS,
} = await import("@/lib/leads");

describe("LEAD_STAGES / LEAD_STATUSES", () => {
  it("matches the five canonical stages from pipeline.py exactly", () => {
    expect(new Set(LEAD_STAGES)).toEqual(
      new Set(["captured", "qualified", "contacted", "converted", "disqualified"])
    );
    expect(LEAD_STAGES).toHaveLength(5);
  });

  it("matches the four canonical statuses from pipeline.py exactly", () => {
    expect(new Set(LEAD_STATUSES)).toEqual(new Set(["new", "open", "won", "lost"]));
    expect(LEAD_STATUSES).toHaveLength(4);
  });
});

describe("buildLeadsQuery", () => {
  it("page=1, no filters -> limit=25&offset=0", () => {
    const qs = buildLeadsQuery({ page: 1 });
    const params = new URLSearchParams(qs);
    expect(params.get("limit")).toBe("25");
    expect(params.get("offset")).toBe("0");
    expect(params.has("stage")).toBe(false);
  });

  it("page=3 -> offset=50", () => {
    const params = new URLSearchParams(buildLeadsQuery({ page: 3 }));
    expect(params.get("offset")).toBe("50");
  });

  it("page=0 or negative -> clamped to offset=0", () => {
    expect(new URLSearchParams(buildLeadsQuery({ page: 0 })).get("offset")).toBe("0");
    expect(new URLSearchParams(buildLeadsQuery({ page: -5 })).get("offset")).toBe("0");
  });

  it("a valid stage is included", () => {
    const params = new URLSearchParams(buildLeadsQuery({ page: 1, stage: "qualified" }));
    expect(params.get("stage")).toBe("qualified");
  });

  it("an unknown stage is dropped", () => {
    const params = new URLSearchParams(buildLeadsQuery({ page: 1, stage: "bogus" }));
    expect(params.has("stage")).toBe(false);
  });

  it("a blank stage is dropped", () => {
    const params = new URLSearchParams(buildLeadsQuery({ page: 1, stage: "" }));
    expect(params.has("stage")).toBe(false);
  });

  it("includes valid sort, direction, filters, and a literal search", () => {
    const params = new URLSearchParams(
      buildLeadsQuery({
        page: 2,
        stage: "qualified",
        status: "open",
        assignedAgentId: "agent-1",
        createdFrom: "2026-01-01T00:00:00.000Z",
        createdTo: "2026-02-01T00:00:00.000Z",
        q: "Alice",
        sort: "score",
        direction: "desc",
      })
    );

    expect(params.get("offset")).toBe("25");
    expect(params.get("status")).toBe("open");
    expect(params.get("assigned_agent_id")).toBe("agent-1");
    expect(params.get("from")).toBe("2026-01-01T00:00:00.000Z");
    expect(params.get("to")).toBe("2026-02-01T00:00:00.000Z");
    expect(params.get("q")).toBe("Alice");
    expect(params.get("sort")).toBe("score");
    expect(params.get("dir")).toBe("desc");
  });

  it("drops unknown sort, a direction without sort, and invalid search", () => {
    const params = new URLSearchParams(
      buildLeadsQuery({ page: 1, sort: "tenant_id", direction: "asc", q: "a" })
    );

    expect(params.has("sort")).toBe(false);
    expect(params.has("dir")).toBe(false);
    expect(params.has("q")).toBe(false);
  });

  it("has the same seven sort keys as the backend contract", () => {
    expect(LEAD_SORT_KEYS).toEqual([
      "name",
      "email",
      "stage",
      "status",
      "score",
      "assigned",
      "created",
    ]);
  });

  it("URL-encodes values rather than string-concatenating", () => {
    const qs = buildLeadsQuery({ page: 1, stage: "qualified" });
    expect(qs).toMatch(/^limit=25&offset=0&stage=qualified$/);
  });
});

describe("listLeads", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope to an ok result with items/total passed through, no tenant_id", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const body = {
      items: [
        {
          lead_id: "lead-1",
          name: "Ada Lovelace",
          email: "ada@example.com",
          phone: null,
          status: "new",
          stage: "captured",
          qualification_score: null,
          assigned_agent_id: null,
          source: "widget",
          created_at: "2026-07-15T00:00:00Z",
        },
      ],
      total: 57,
      limit: 25,
      offset: 0,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 })
    );

    const result = await listLeads({ page: 1 });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.total).toBe(57);
      expect(result.items).toHaveLength(1);
      expect(result.items[0].leadId).toBe("lead-1");
      expect(result.items[0]).not.toHaveProperty("tenant_id");
      expect(result.items[0]).not.toHaveProperty("tenantId");
    }
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "ROLE_NOT_PERMITTED",
          message: "nope",
          correlation_id: "corr-1",
        }),
        { status: 403 }
      )
    );

    const result = await listLeads({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
      expect(result.correlationId).toBe("corr-1");
    }
  });

  it("maps a 401 to a session-expired message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "AUTHENTICATION_ERROR", message: "x", correlation_id: "c" }),
        { status: 401 }
      )
    );

    const result = await listLeads({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
    }
  });

  it("maps a 422 INVALID_LEAD_FILTER to a friendly filter banner", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "INVALID_LEAD_FILTER", message: "bad", correlation_id: "c" }),
        { status: 422 }
      )
    );

    const result = await listLeads({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/filter/i);
    }
  });

  it("maps a 422 INVALID_LIST_WINDOW to a friendly filter banner", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "INVALID_LIST_WINDOW", message: "bad", correlation_id: "c" }),
        { status: 422 }
      )
    );

    const result = await listLeads({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/filter/i);
    }
  });

  it("maps an unknown error code to a generic message including the correlation id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "SOMETHING_ELSE", message: "x", correlation_id: "corr-xyz" }),
        { status: 500 }
      )
    );

    const result = await listLeads({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-xyz");
      expect(result.message).toContain("corr-xyz");
    }
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await listLeads({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });

  it("never logs the response body", async () => {
    getMock.mockReturnValue(undefined);
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              lead_id: "lead-1",
              name: "Secret Name",
              email: "secret@example.com",
              phone: "+15551234567",
              status: "new",
              stage: "captured",
              qualification_score: 10,
              assigned_agent_id: null,
              source: "widget",
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

    await listLeads({ page: 1 });

    expect(consoleSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("targets the implicit /admin/leads path when tenantId is omitted", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 })
    );

    await listLeads({ page: 1 });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/leads?limit=25&offset=0");
  });

  it("targets the S12.7 tenant-scoped path when tenantId is provided (PLATFORM_ADMIN)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 })
    );

    await listLeads({ page: 1 }, "tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/tenants/tenant-x/leads?limit=25&offset=0");
  });
});

describe("getLeadDetail", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope to an ok result with no tenant_id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          lead_id: "lead-1",
          name: "Ada Lovelace",
          email: "ada@example.com",
          phone: null,
          status: "open",
          stage: "qualified",
          qualification_score: 82,
          assigned_agent_id: null,
          source: "widget",
        }),
        { status: 200 }
      )
    );

    const result = await getLeadDetail("lead-1");
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.lead.leadId).toBe("lead-1");
      expect(result.lead).not.toHaveProperty("tenant_id");
    }
  });

  it("maps a 404 to a not-found message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "NOT_FOUND", message: "x", correlation_id: "c" }),
        { status: 404 }
      )
    );

    const result = await getLeadDetail("lead-1");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/not be found/i);
    }
  });

  it("targets the tenant-scoped path when tenantId is provided", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          lead_id: "lead-1",
          name: "Ada",
          email: "ada@example.com",
          phone: null,
          status: "new",
          stage: "captured",
          qualification_score: null,
          assigned_agent_id: null,
          source: "widget",
        }),
        { status: 200 }
      )
    );

    await getLeadDetail("lead-1", "tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/tenants/tenant-x/leads/lead-1");
  });
});

describe("getLeadActivities", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 array to an ok result with items mapped", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            activity_id: "act-1",
            lead_id: "lead-1",
            type: "note",
            payload: { text: "Called, left voicemail" },
            actor: "agent-1",
            created_at: "2026-07-15T00:00:00Z",
          },
        ]),
        { status: 200 }
      )
    );

    const result = await getLeadActivities("lead-1");
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items).toHaveLength(1);
      expect(result.items[0].activityId).toBe("act-1");
      expect(result.items[0].type).toBe("note");
    }
  });

  it("maps a network throw to a generic message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await getLeadActivities("lead-1");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });
});

describe("stageBadgeStyle", () => {
  it("returns the SR-15 monochrome-restyle colors for each canonical stage (citron deleted per D1)", () => {
    expect(stageBadgeStyle("captured")).toEqual({ label: "CAPTURED", bg: "#efeee6", fg: "var(--ink-2)" });
    expect(stageBadgeStyle("qualified")).toEqual({ label: "QUALIFIED", bg: "#efeee6", fg: "#333333" });
    expect(stageBadgeStyle("contacted")).toEqual({ label: "CONTACTED", bg: "#efeee6", fg: "#404040" });
    expect(stageBadgeStyle("converted")).toEqual({ label: "CONVERTED", bg: "#eaf3ec", fg: "#3f7d57" });
    expect(stageBadgeStyle("disqualified")).toEqual({
      label: "DISQUALIFIED",
      bg: "#f6e3df",
      fg: "#a24b4b",
    });
  });

  it("falls back to a neutral style for an unrecognized stage rather than throwing", () => {
    const style = stageBadgeStyle("bogus");
    expect(style.label).toBe("BOGUS");
    expect(style.bg).toBe("#ecece5");
  });
});

describe("scoreChipStyle", () => {
  // SR-24 item 15: the legacy citron hex (#eef7a8) is retired -- a score
  // >=60 on a non-converted stage now uses the design system's success
  // token pair (globals.css's --success-bg/--success-fg), not a one-off hex.
  it("uses the success-token chip for a score >= 60 on a non-converted stage", () => {
    expect(scoreChipStyle(76, "contacted")).toEqual({
      label: "76",
      bg: "var(--success-bg)",
      fg: "var(--success-fg)",
    });
  });

  it("uses the converted-green chip regardless of score once stage is converted", () => {
    expect(scoreChipStyle(90, "converted")).toEqual({ label: "90", bg: "#dcefdc", fg: "#1f6a2f" });
  });

  it("uses a plain muted style below the 60 threshold", () => {
    expect(scoreChipStyle(25, "disqualified")).toEqual({ label: "25", bg: "transparent", fg: "var(--muted-foreground)" });
  });

  it("the 60 boundary itself is highlighted (>= not >)", () => {
    expect(scoreChipStyle(60, "qualified").bg).toBe("var(--success-bg)");
    expect(scoreChipStyle(59, "qualified").bg).toBe("transparent");
  });
});

describe("initialsFromName", () => {
  it("takes the first letter of the first two words", () => {
    expect(initialsFromName("Sara Romero")).toBe("SR");
  });

  it("uppercases a lowercase name", () => {
    expect(initialsFromName("jordan millar")).toBe("JM");
  });

  it("falls back to the first two letters for a single-word name", () => {
    expect(initialsFromName("Cher")).toBe("CH");
  });

  it("falls back to '?' for a blank name rather than throwing", () => {
    expect(initialsFromName("   ")).toBe("?");
  });
});
