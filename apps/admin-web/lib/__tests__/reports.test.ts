import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const {
  resolveReportQuery,
  resolveBookingsQuery,
  getLeadsByStageReport,
  getBookingsReport,
  getFunnelReport,
  getWinLossReport,
  getLeadSourcesReport,
  getScoreDistributionReport,
  getAgentPerformanceReport,
  getRecentConversionsReport,
  reportCsvPath,
} = await import("@/lib/reports");

describe("resolveReportQuery", () => {
  it("default (no params) -> a ~30-day span", () => {
    const params = new URLSearchParams(resolveReportQuery({}));
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(30, 0);
  });

  it("range=7d -> a ~7-day span", () => {
    const params = new URLSearchParams(resolveReportQuery({ range: "7d" }));
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(7, 0);
  });

  it("an unknown range falls back to the 30-day default", () => {
    const params = new URLSearchParams(resolveReportQuery({ range: "bogus" }));
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(30, 0);
  });

  it("passes through source/status/assignedAgentId/ownerAgentId filters verbatim", () => {
    const params = new URLSearchParams(
      resolveReportQuery({
        source: "referral",
        status: "booked",
        assignedAgentId: "agent-1",
        ownerAgentId: "agent-2",
      })
    );
    expect(params.get("source")).toBe("referral");
    expect(params.get("status")).toBe("booked");
    expect(params.get("assigned_agent_id")).toBe("agent-1");
    expect(params.get("owner_agent_id")).toBe("agent-2");
  });

  it("omits filters that were not supplied", () => {
    const params = new URLSearchParams(resolveReportQuery({}));
    expect(params.has("source")).toBe(false);
    expect(params.has("assigned_agent_id")).toBe(false);
  });
});

describe("resolveBookingsQuery", () => {
  it("defaults bucket to day", () => {
    const params = new URLSearchParams(resolveBookingsQuery({}));
    expect(params.get("bucket")).toBe("day");
  });

  it("bucket=month is honored (SR-9.5 D3)", () => {
    const params = new URLSearchParams(resolveBookingsQuery({ bucket: "month" }));
    expect(params.get("bucket")).toBe("month");
  });

  it("bucket=week is honored", () => {
    const params = new URLSearchParams(resolveBookingsQuery({ bucket: "week" }));
    expect(params.get("bucket")).toBe("week");
  });

  it("an unknown bucket falls back to day (guards against INVALID_BUCKET)", () => {
    const params = new URLSearchParams(resolveBookingsQuery({ bucket: "hour" }));
    expect(params.get("bucket")).toBe("day");
  });
});

describe("reportCsvPath", () => {
  it("points at this app's own proxy route, not admin-api directly", () => {
    const path = reportCsvPath("win-loss", "from=a&to=b");
    expect(path).toMatch(/^\/reports\/csv\/win-loss\?/);
  });

  it("carries tenant_id as a query param for the PLATFORM_ADMIN variant", () => {
    const path = reportCsvPath("funnel", "from=a&to=b", "tenant-xyz");
    const params = new URLSearchParams(path.split("?")[1]);
    expect(params.get("tenant_id")).toBe("tenant-xyz");
  });

  it("omits tenant_id when not provided", () => {
    const path = reportCsvPath("bookings", "from=a&to=b");
    const params = new URLSearchParams(path.split("?")[1]);
    expect(params.has("tenant_id")).toBe(false);
  });
});

describe("getLeadsByStageReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 body to an ok result with every stage key present", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          stages: { captured: 3, qualified: 1, contacted: 0, converted: 2, disqualified: 0 },
          total: 6,
        }),
        { status: 200 }
      )
    );

    const result = await getLeadsByStageReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.stages.captured).toBe(3);
      expect(result.data.stages.disqualified).toBe(0);
      expect(result.data.total).toBe(6);
    }
  });

  it("maps a 403 to a friendly permission message with correlation id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "corr-9" }),
        { status: 403 }
      )
    );

    const result = await getLeadsByStageReport({});
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
      expect(result.correlationId).toBe("corr-9");
    }
  });

  it("a network throw (not AdminApiError) maps to an honest unreachable message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("ECONNREFUSED"));

    const result = await getLeadsByStageReport({});
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });
});

describe("getBookingsReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps series + totals, camelCased, cancelled visible", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z", bucket: "week" },
          series: [
            {
              bucket_start: "2026-07-06T00:00:00Z",
              booked: 2,
              completed: 1,
              no_show: 0,
              cancelled: 1,
              total_excluding_cancelled: 3,
            },
          ],
          totals: { booked: 2, completed: 1, no_show: 0, cancelled: 1, total_excluding_cancelled: 3 },
        }),
        { status: 200 }
      )
    );

    const result = await getBookingsReport({ bucket: "week" });
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.series[0].cancelled).toBe(1);
      expect(result.data.series[0].noShow).toBe(0);
      expect(result.data.totals.totalExcludingCancelled).toBe(3);
      expect(result.data.window.bucket).toBe("week");
    }
  });
});

describe("getFunnelReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("preserves null drop_off_rate and overall_conversion_rate (never coerced to 0)", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          steps: [
            { stage: "captured", count: 0, drop_off_rate: null },
            { stage: "qualified", count: 0, drop_off_rate: null },
            { stage: "contacted", count: 0, drop_off_rate: null },
            { stage: "converted", count: 0, drop_off_rate: null },
          ],
          disqualified: { count: 0 },
          overall_conversion_rate: null,
        }),
        { status: 200 }
      )
    );

    const result = await getFunnelReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.overallConversionRate).toBeNull();
      expect(result.data.steps[0].dropOffRate).toBeNull();
      expect(result.data.disqualifiedCount).toBe(0);
    }
  });

  it("disqualified is a separate field, never appears inside steps", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          steps: [
            { stage: "captured", count: 5, drop_off_rate: null },
            { stage: "qualified", count: 3, drop_off_rate: 0.4 },
            { stage: "contacted", count: 2, drop_off_rate: 0.3333 },
            { stage: "converted", count: 1, drop_off_rate: 0.5 },
          ],
          disqualified: { count: 2 },
          overall_conversion_rate: 0.2,
        }),
        { status: 200 }
      )
    );

    const result = await getFunnelReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.steps.map((s) => s.stage)).not.toContain("disqualified");
      expect(result.data.disqualifiedCount).toBe(2);
    }
  });
});

describe("getWinLossReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps Decimal-string amounts and null avg_deal_size verbatim (never coerced)", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          currency: "USD",
          currency_configured: false,
          won: {
            count: 1,
            amount_total: "12500.00",
            amount_null_count: 0,
            avg_deal_size: "12500.00",
            avg_days_to_close: 9.0,
          },
          lost: {
            count: 1,
            amount_total: "0",
            amount_null_count: 1,
            avg_deal_size: null,
            avg_days_to_close: 10.0,
          },
          win_rate: 0.5,
          loss_reasons: [{ closed_at: "2026-07-12T00:00:00Z", close_reason: "Went with competitor" }],
        }),
        { status: 200 }
      )
    );

    const result = await getWinLossReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.won.amountTotal).toBe("12500.00");
      expect(result.data.lost.avgDealSize).toBeNull();
      expect(result.data.currencyConfigured).toBe(false);
      expect(result.data.lossReasons).toHaveLength(1);
      expect(result.data.lossReasons[0].closeReason).toBe("Went with competitor");
      // win_probability must never appear anywhere in this report (D13).
      expect(result.data).not.toHaveProperty("winProbability");
      expect(result.data.won).not.toHaveProperty("winProbability");
    }
  });

  it("maps a 404 TENANT_NOT_FOUND to a tenant-specific message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "TENANT_NOT_FOUND", message: "nope", correlation_id: "corr-4" }),
        { status: 404 }
      )
    );

    const result = await getWinLossReport({}, "does-not-exist");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/tenant/i);
    }
  });
});

// ==============================================================================
// SR-19: lead-sources, score-distribution, agent-performance,
// recent-conversions
// ==============================================================================

describe("getLeadSourcesReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps single_source -> singleSource and does not pad sources (D4)", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          sources: [{ source: "widget", count: 5, percentage: 100.0 }],
          total: 5,
          single_source: true,
        }),
        { status: 200 }
      )
    );

    const result = await getLeadSourcesReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.sources).toHaveLength(1);
      expect(result.data.singleSource).toBe(true);
      expect(result.data.total).toBe(5);
    }
  });

  it("multiple sources -> singleSource false", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          sources: [
            { source: "widget", count: 3, percentage: 60.0 },
            { source: "referral", count: 2, percentage: 40.0 },
          ],
          total: 5,
          single_source: false,
        }),
        { status: 200 }
      )
    );

    const result = await getLeadSourcesReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.sources).toHaveLength(2);
      expect(result.data.singleSource).toBe(false);
    }
  });
});

describe("getScoreDistributionReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps all five bands + a separate unscored count (D8)", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          bands: { "0-19": 1, "20-39": 0, "40-59": 2, "60-79": 0, "80-100": 1 },
          unscored: 3,
          total: 7,
        }),
        { status: 200 }
      )
    );

    const result = await getScoreDistributionReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.bands["0-19"]).toBe(1);
      expect(result.data.unscored).toBe(3);
      expect(result.data.total).toBe(7);
    }
  });
});

describe("getAgentPerformanceReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("preserves null win_rate (never coerced to 0) and maps the unassigned row (D7)", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          agents: [
            { assigned_agent_id: "agent-1", assigned: 3, contacted: 0, won: 0, win_rate: null },
            { assigned_agent_id: "agent-2", assigned: 4, contacted: 2, won: 1, win_rate: 0.5 },
          ],
          unassigned: { assigned_agent_id: null, assigned: 1, contacted: 0, won: 0, win_rate: null },
        }),
        { status: 200 }
      )
    );

    const result = await getAgentPerformanceReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.agents[0].winRate).toBeNull();
      expect(result.data.agents[1].winRate).toBe(0.5);
      expect(result.data.unassigned.assignedAgentId).toBeNull();
      expect(result.data.unassigned.assigned).toBe(1);
    }
  });
});

describe("getRecentConversionsReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps conversions with no value field anywhere (D6/M5)", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          conversions: [
            {
              lead_id: "lead-1",
              name: "Jane Doe",
              source: "widget",
              stage: "converted",
              converted_at: "2026-07-15T00:00:00Z",
            },
          ],
        }),
        { status: 200 }
      )
    );

    const result = await getRecentConversionsReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.conversions).toHaveLength(1);
      expect(result.data.conversions[0].leadId).toBe("lead-1");
      expect(result.data.conversions[0]).not.toHaveProperty("value");
    }
  });

  it("empty window -> an empty conversions array, not an error", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          window: { from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z" },
          conversions: [],
        }),
        { status: 200 }
      )
    );

    const result = await getRecentConversionsReport({});
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.conversions).toEqual([]);
    }
  });
});

describe("reportCsvPath (SR-19 reports)", () => {
  it("supports the four new report names", () => {
    for (const report of [
      "lead-sources",
      "score-distribution",
      "agent-performance",
      "recent-conversions",
    ] as const) {
      const path = reportCsvPath(report, "from=a&to=b");
      expect(path).toMatch(new RegExp(`^/reports/csv/${report}\\?`));
    }
  });
});
