import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const { getChatbotHub, resolveHubQuery } = await import("@/lib/hub");

const overview = {
  window: { from: "2026-07-01T00:00:00.000Z", to: "2026-07-21T00:00:00.000Z", bucket: "day" },
  totals: { conversations: 4, user_turns: 8, bot_turns: 8, decided_bot_turns: 6 },
  intent_distribution: { pricing: 2 },
  decision_distribution: { answer: 3, escalate: 1, clarify: 0, blocked: 0 },
  fallback_rate: null,
  deflection_rate: null,
  grounded_rate: 1,
  schedule: { cta_conversations: 1, conversions: 0, conversion_rate: null },
  series: [
    {
      bucket_start: "2026-07-20T00:00:00.000Z",
      conversations: 4,
      answers: 3,
      escalations: 1,
      bookings: 0,
    },
  ],
};

describe("getChatbotHub", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("composes only the existing analytics overview, active-conversation, and closed-conversation facts", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(overview), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              {
                conversation_id: "conversation-1",
                status: "active",
                channel: "widget",
                visitor_id: null,
                started_at: "2026-07-20T10:00:00.000Z",
                ended_at: null,
                message_count: 4,
                summary: "Asked about pricing",
              },
            ],
            total: 2,
            limit: 1,
            offset: 0,
          }),
          { status: 200 }
        )
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], total: 26, limit: 1, offset: 0 }), { status: 200 })
      );

    const result = await getChatbotHub({ period: "month", bucket: "week" });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.analytics.deflectionRate).toBeNull();
      expect(result.data.analytics.decisionDistribution.answer).toBe(3);
      expect(result.data.activeConversations.total).toBe(2);
      expect(result.data.activeConversations.items[0]?.summary).toBe("Asked about pricing");
      expect(result.data.closedConversations.total).toBe(26);
    }
    expect(fetchSpy).toHaveBeenCalledTimes(3);
    const firstUrl = new URL(fetchSpy.mock.calls[0]?.[0] as string);
    const secondUrl = new URL(fetchSpy.mock.calls[1]?.[0] as string);
    const thirdUrl = new URL(fetchSpy.mock.calls[2]?.[0] as string);
    expect(firstUrl.pathname).toBe("/admin/analytics/overview");
    expect(firstUrl.searchParams.get("bucket")).toBe("week");
    expect(secondUrl.pathname).toBe("/admin/conversations");
    expect(secondUrl.searchParams.get("status")).toBe("active");
    // SR-12 D3: the active-conversations read now fetches up to 3 items so the
    // "Answered by chatbot" card can render an honest multi-row activity list.
    expect(secondUrl.searchParams.get("limit")).toBe("3");
    expect(thirdUrl.pathname).toBe("/admin/conversations");
    expect(thirdUrl.searchParams.get("status")).toBe("ended");
    expect(thirdUrl.searchParams.get("limit")).toBe("1");
  });

  it("maps all 3 active-conversation items through unchanged (no padding, no fabrication) when the endpoint returns 3", async () => {
    getMock.mockReturnValue(undefined);
    const threeItems = [0, 1, 2].map((n) => ({
      conversation_id: `conversation-${n}`,
      status: "active",
      channel: "widget",
      visitor_id: null,
      started_at: `2026-07-20T1${n}:00:00.000Z`,
      ended_at: null,
      message_count: n + 1,
      summary: n === 1 ? null : `Summary ${n}`,
    }));
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(overview), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: threeItems, total: 3, limit: 3, offset: 0 }), { status: 200 })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0 }), { status: 200 })
      );

    const result = await getChatbotHub({ period: "month", bucket: "day" });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      const items = result.data.activeConversations.items;
      expect(items).toHaveLength(3);
      expect(items[0]?.conversationId).toBe("conversation-0");
      expect(items[1]?.summary).toBeNull();
      expect(items[2]?.messageCount).toBe(3);
    }
  });

  it("returns exactly the real number of active items (1) without padding to 3", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(overview), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              {
                conversation_id: "conversation-solo",
                status: "active",
                channel: "widget",
                visitor_id: null,
                started_at: "2026-07-20T10:00:00.000Z",
                ended_at: null,
                message_count: 2,
                summary: null,
              },
            ],
            total: 1,
            limit: 3,
            offset: 0,
          }),
          { status: 200 }
        )
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0 }), { status: 200 })
      );

    const result = await getChatbotHub({ period: "month", bucket: "day" });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.activeConversations.items).toHaveLength(1);
    }
  });

  it("returns 0 (not fabricated) closed conversations for a zero tenant", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...overview, totals: { ...overview.totals, conversations: 0 }, series: [] }), {
          status: 200,
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0 }), { status: 200 })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0 }), { status: 200 })
      );

    const result = await getChatbotHub({ period: "month", bucket: "day" });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.closedConversations.total).toBe(0);
    }
  });

  it("returns an explicit error with correlation id rather than fabricated hub values", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({ error_code: "INTERNAL_SERVER_ERROR", message: "unavailable", correlation_id: "corr-hub" }),
        { status: 500 }
      )
    );

    const result = await getChatbotHub({ period: "month", bucket: "day" });

    expect(result).toEqual({
      status: "error",
      message: expect.stringMatching(/could not be loaded/i),
      correlationId: "corr-hub",
    });
    expect("data" in result).toBe(false);
  });

  it("returns the error hub (no partial/fabricated data) when only the closed-conversations read fails", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(overview), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], total: 2, limit: 1, offset: 0 }), { status: 200 })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ error_code: "INTERNAL_SERVER_ERROR", message: "unavailable", correlation_id: "corr-closed" }),
          { status: 500 }
        )
      );

    const result = await getChatbotHub({ period: "month", bucket: "day" });

    expect(result.status).toBe("error");
    expect("data" in result).toBe(false);
  });

  it("keeps the URL window server-owned and rejects unsupported period/bucket values", () => {
    const query = new URLSearchParams(resolveHubQuery({ period: "year", bucket: "week" }));
    expect(query.get("bucket")).toBe("week");
    expect(query.get("from")).toBeTruthy();
    expect(query.get("to")).toBeTruthy();

    const fallbackQuery = new URLSearchParams(resolveHubQuery({ period: "unknown", bucket: "month" }));
    expect(fallbackQuery.get("bucket")).toBe("day");
  });
});
