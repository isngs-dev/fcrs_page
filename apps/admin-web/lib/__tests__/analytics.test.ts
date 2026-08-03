import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

// Imported after the mock is registered so the module under test picks up
// the mocked `next/headers` (adminApiFetch reads the access_token cookie).
const { resolveAnalyticsQuery, getAnalyticsOverview, formatRate, ANALYTICS_BUCKETS } =
  await import("@/lib/analytics");

describe("ANALYTICS_BUCKETS", () => {
  it("matches the canonical buckets from repository.py exactly (SR-9.5 D3: month added)", () => {
    expect(ANALYTICS_BUCKETS).toEqual(["day", "week", "month"]);
  });
});

describe("resolveAnalyticsQuery", () => {
  it("default (no params) -> bucket=day and a ~30-day span", () => {
    const params = new URLSearchParams(resolveAnalyticsQuery({}));
    expect(params.get("bucket")).toBe("day");
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(30, 0);
    expect(from.getTime()).toBeLessThan(to.getTime());
  });

  it("range=7d -> a ~7-day span", () => {
    const params = new URLSearchParams(resolveAnalyticsQuery({ range: "7d" }));
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(7, 0);
  });

  it("range=90d -> a ~90-day span", () => {
    const params = new URLSearchParams(resolveAnalyticsQuery({ range: "90d" }));
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(90, 0);
  });

  it("an unknown range falls back to the 30-day default (never emitted raw)", () => {
    const params = new URLSearchParams(resolveAnalyticsQuery({ range: "bogus" }));
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(30, 0);
  });

  it("bucket=week is included", () => {
    const params = new URLSearchParams(resolveAnalyticsQuery({ bucket: "week" }));
    expect(params.get("bucket")).toBe("week");
  });

  it("bucket=month is included (SR-9.5 D3 widened contract)", () => {
    const params = new URLSearchParams(resolveAnalyticsQuery({ bucket: "month" }));
    expect(params.get("bucket")).toBe("month");
  });

  it("an unknown bucket=hour falls back to day (guards against INVALID_BUCKET)", () => {
    const params = new URLSearchParams(resolveAnalyticsQuery({ bucket: "hour" }));
    expect(params.get("bucket")).toBe("day");
  });

  it("from < to always, and values are URL-encoded via URLSearchParams", () => {
    const qs = resolveAnalyticsQuery({ range: "7d", bucket: "week" });
    const params = new URLSearchParams(qs);
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    expect(from.getTime()).toBeLessThan(to.getTime());
    // ISO datetimes contain `:` which URLSearchParams encodes as %3A.
    expect(qs).toContain("%3A");
  });

  it("range=custom with valid from/to honors the caller-supplied window verbatim", () => {
    const qs = resolveAnalyticsQuery({
      range: "custom",
      from: "2026-01-01",
      to: "2026-01-15",
    });
    const params = new URLSearchParams(qs);
    expect(new Date(params.get("from")!).toISOString()).toBe(
      new Date("2026-01-01").toISOString()
    );
    expect(new Date(params.get("to")!).toISOString()).toBe(new Date("2026-01-15").toISOString());
  });

  it("range=custom with from >= to falls back to the 30-day default (no silent misapplication)", () => {
    const qs = resolveAnalyticsQuery({ range: "custom", from: "2026-01-15", to: "2026-01-01" });
    const params = new URLSearchParams(qs);
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(30, 0);
  });

  it("range=custom with a missing to falls back to the 30-day default", () => {
    const qs = resolveAnalyticsQuery({ range: "custom", from: "2026-01-01" });
    const params = new URLSearchParams(qs);
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(30, 0);
  });

  it("range=custom with an unparseable date falls back to the 30-day default", () => {
    const qs = resolveAnalyticsQuery({ range: "custom", from: "not-a-date", to: "2026-01-15" });
    const params = new URLSearchParams(qs);
    const from = new Date(params.get("from")!);
    const to = new Date(params.get("to")!);
    const days = (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000);
    expect(days).toBeCloseTo(30, 0);
  });
});

describe("formatRate (Decision 6a, MANDATORY no-silent-fallback)", () => {
  it("null -> 'No data' (never '0%')", () => {
    expect(formatRate(null)).toBe("No data");
  });

  it("0 -> '0%'", () => {
    expect(formatRate(0)).toBe("0%");
  });

  it("0.4213 -> '42.1%'", () => {
    expect(formatRate(0.4213)).toBe("42.1%");
  });

  it("0.5 -> '50%'", () => {
    expect(formatRate(0.5)).toBe("50%");
  });

  it("1 -> '100%'", () => {
    expect(formatRate(1)).toBe("100%");
  });
});

describe("getAnalyticsOverview", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  function makeBody(overrides: Partial<Record<string, unknown>> = {}) {
    return {
      window: { from: "2026-06-16T00:00:00Z", to: "2026-07-16T00:00:00Z", bucket: "day" },
      totals: { conversations: 10, user_turns: 40, bot_turns: 40, decided_bot_turns: 35 },
      intent_distribution: { pricing: 5, unclassified: 2 },
      decision_distribution: { answer: 20, escalate: 3 },
      fallback_rate: 0.1,
      deflection_rate: null,
      grounded_rate: 0.8,
      schedule: { cta_conversations: 4, conversions: 0, conversion_rate: null },
      series: [
        { bucket_start: "2026-07-15T00:00:00Z", conversations: 2, answers: 2, escalations: 0, bookings: 0 },
      ],
      ...overrides,
    };
  }

  it("maps a 200 nested body to an ok result, camelCased, null rates preserved", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(makeBody()), { status: 200 })
    );

    const result = await getAnalyticsOverview({});

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.totals.conversations).toBe(10);
      expect(result.data.totals.userTurns).toBe(40);
      expect(result.data.fallbackRate).toBe(0.1);
      // The load-bearing assertion: a null denominator stays null, never 0.
      expect(result.data.deflectionRate).toBeNull();
      expect(result.data.schedule.conversionRate).toBeNull();
      expect(result.data.groundedRate).toBe(0.8);
      expect(result.data.series).toHaveLength(1);
      expect(result.data).not.toHaveProperty("tenant_id");
      expect(result.data).not.toHaveProperty("visitor_id");
      expect(result.data).not.toHaveProperty("conversation_id");
    }
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "corr-1" }),
        { status: 403 }
      )
    );

    const result = await getAnalyticsOverview({});
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

    const result = await getAnalyticsOverview({});
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
    }
  });

  it("maps a 422 INVALID_ANALYTICS_WINDOW to a friendly window banner", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "INVALID_ANALYTICS_WINDOW", message: "bad", correlation_id: "c" }),
        { status: 422 }
      )
    );

    const result = await getAnalyticsOverview({});
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/range|bucket/i);
    }
  });

  it("maps a 422 ANALYTICS_WINDOW_TOO_LARGE to a friendly window banner", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ANALYTICS_WINDOW_TOO_LARGE", message: "bad", correlation_id: "c" }),
        { status: 422 }
      )
    );

    const result = await getAnalyticsOverview({});
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/range|bucket/i);
    }
  });

  it("maps a 422 INVALID_BUCKET to a friendly window banner", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "INVALID_BUCKET", message: "bad", correlation_id: "c" }),
        { status: 422 }
      )
    );

    const result = await getAnalyticsOverview({});
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/range|bucket/i);
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

    const result = await getAnalyticsOverview({});
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-xyz");
      expect(result.message).toContain("corr-xyz");
    }
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await getAnalyticsOverview({});
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
      new Response(JSON.stringify(makeBody()), { status: 200 })
    );

    await getAnalyticsOverview({});

    expect(consoleSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("targets the implicit /admin/analytics/overview path when tenantId is omitted", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(makeBody()), { status: 200 }));

    await getAnalyticsOverview({});

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url.startsWith("http://localhost:8000/admin/analytics/overview?")).toBe(true);
  });

  it("targets the S12.7 tenant-scoped path when tenantId is provided (PLATFORM_ADMIN)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(makeBody()), { status: 200 }));

    await getAnalyticsOverview({}, "tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(
      url.startsWith("http://localhost:8000/admin/tenants/tenant-x/analytics/overview?")
    ).toBe(true);
  });
});
