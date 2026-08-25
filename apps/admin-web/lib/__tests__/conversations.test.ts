import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

// Imported after the mock is registered so the module under test picks up
// the mocked `next/headers` (adminApiFetch reads the access_token cookie).
const {
  buildConversationsQuery,
  listConversations,
  getConversationDetail,
  getMessageSources,
  CONVERSATION_STATUSES,
} = await import("@/lib/conversations");

describe("CONVERSATION_STATUSES", () => {
  it("matches the two canonical statuses from admin_routes.py exactly", () => {
    expect(new Set(CONVERSATION_STATUSES)).toEqual(new Set(["active", "ended"]));
    expect(CONVERSATION_STATUSES).toHaveLength(2);
  });
});

describe("buildConversationsQuery", () => {
  it("page=1, no filters -> limit=25&offset=0", () => {
    const qs = buildConversationsQuery({ page: 1 });
    const params = new URLSearchParams(qs);
    expect(params.get("limit")).toBe("25");
    expect(params.get("offset")).toBe("0");
    expect(params.has("status")).toBe(false);
  });

  it("page=3 -> offset=50", () => {
    const params = new URLSearchParams(buildConversationsQuery({ page: 3 }));
    expect(params.get("offset")).toBe("50");
  });

  it("page=0 or negative -> clamped to offset=0", () => {
    expect(new URLSearchParams(buildConversationsQuery({ page: 0 })).get("offset")).toBe("0");
    expect(new URLSearchParams(buildConversationsQuery({ page: -5 })).get("offset")).toBe("0");
  });

  it("a valid status is included", () => {
    const params = new URLSearchParams(buildConversationsQuery({ page: 1, status: "active" }));
    expect(params.get("status")).toBe("active");
  });

  it("an unknown status (e.g. the mock's 'live'/'lead') is dropped", () => {
    const params = new URLSearchParams(buildConversationsQuery({ page: 1, status: "live" }));
    expect(params.has("status")).toBe(false);
  });

  it("a blank status is dropped", () => {
    const params = new URLSearchParams(buildConversationsQuery({ page: 1, status: "" }));
    expect(params.has("status")).toBe(false);
  });

  it("URL-encodes values rather than string-concatenating", () => {
    const qs = buildConversationsQuery({ page: 1, status: "ended" });
    expect(qs).toMatch(/^limit=25&offset=0&status=ended$/);
  });
});

describe("listConversations", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope to an ok result with items/total passed through, no tenant_id", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const body = {
      items: [
        {
          conversation_id: "conv-1",
          status: "active",
          channel: "widget",
          visitor_id: "4821",
          started_at: "2026-07-19T00:00:00Z",
          ended_at: null,
          message_count: 4,
          summary: "Pricing question",
        },
      ],
      total: 12,
      limit: 25,
      offset: 0,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 })
    );

    const result = await listConversations({ page: 1 });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.total).toBe(12);
      expect(result.items).toHaveLength(1);
      expect(result.items[0].conversationId).toBe("conv-1");
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

    const result = await listConversations({ page: 1 });
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

    const result = await listConversations({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
    }
  });

  it("maps a 422 INVALID_CONVERSATION_FILTER to a friendly filter banner", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "INVALID_CONVERSATION_FILTER", message: "bad", correlation_id: "c" }),
        { status: 422 }
      )
    );

    const result = await listConversations({ page: 1 });
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

    const result = await listConversations({ page: 1 });
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

    const result = await listConversations({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-xyz");
      expect(result.message).toContain("corr-xyz");
    }
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await listConversations({ page: 1 });
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
              conversation_id: "conv-1",
              status: "ended",
              channel: "widget",
              visitor_id: "secret-visitor",
              started_at: "2026-07-19T00:00:00Z",
              ended_at: "2026-07-19T00:10:00Z",
              message_count: 2,
              summary: "Secret summary",
            },
          ],
          total: 1,
          limit: 25,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    await listConversations({ page: 1 });

    expect(consoleSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("targets the implicit /admin/conversations path when tenantId is omitted", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 })
    );

    await listConversations({ page: 1 });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/conversations?limit=25&offset=0");
  });

  it("targets the tenant-scoped path when tenantId is provided (PLATFORM_ADMIN)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), { status: 200 })
    );

    await listConversations({ page: 1 }, "tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/tenants/tenant-x/conversations?limit=25&offset=0");
  });
});

describe("getConversationDetail", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope to an ok result with messages mapped, no tenant_id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          conversation_id: "conv-1",
          status: "active",
          channel: "widget",
          started_at: "2026-07-19T00:00:00Z",
          ended_at: null,
          summary: "Pricing question",
          messages: [
            {
              message_id: "msg-1",
              role: "user",
              content: "How much is the team plan?",
              intent: null,
              confidence: null,
              tokens: null,
              created_at: "2026-07-19T00:00:01Z",
              source_count: 0,
            },
            {
              message_id: "msg-2",
              role: "assistant",
              content: "For 40 seats you'd be on the Team plan.",
              intent: "pricing_question",
              confidence: 0.94,
              tokens: 42,
              created_at: "2026-07-19T00:00:05Z",
              source_count: 3,
            },
          ],
        }),
        { status: 200 }
      )
    );

    const result = await getConversationDetail("conv-1");
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.conversation.conversationId).toBe("conv-1");
      expect(result.conversation).not.toHaveProperty("tenant_id");
      expect(result.conversation.messages).toHaveLength(2);
      expect(result.conversation.messages[1].confidence).toBe(0.94);
      expect(result.conversation.messages[1].intent).toBe("pricing_question");
      expect(result.conversation.messages[0].sourceCount).toBe(0);
      expect(result.conversation.messages[1].sourceCount).toBe(3);
    }
  });

  it("maps a 404 CONVERSATION_NOT_FOUND to a not-found message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "CONVERSATION_NOT_FOUND", message: "x", correlation_id: "c" }),
        { status: 404 }
      )
    );

    const result = await getConversationDetail("conv-1");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/not be found/i);
    }
  });

  it("maps a 403 to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "x", correlation_id: "c" }),
        { status: 403 }
      )
    );

    const result = await getConversationDetail("conv-1");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
    }
  });

  it("targets the tenant-scoped path when tenantId is provided", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          conversation_id: "conv-1",
          status: "active",
          channel: "widget",
          started_at: "2026-07-19T00:00:00Z",
          ended_at: null,
          summary: null,
          messages: [],
        }),
        { status: 200 }
      )
    );

    await getConversationDetail("conv-1", "tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/tenants/tenant-x/conversations/conv-1");
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await getConversationDetail("conv-1");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });
});

describe("getMessageSources", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope with resolved sources to an ok result", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          message_id: "msg-2",
          content: "You can run Meta ads alongside lead-gen.",
          decision: "answer",
          confidence: 0.57,
          grounded: true,
          sources: [
            {
              chunk_id: "c1",
              doc_id: "doc-1",
              score: 0.83,
              matched_by: ["vector", "keyword"],
              content: "Real chunk text.",
              resolved: true,
            },
            {
              chunk_id: "c2-deleted",
              doc_id: "doc-2",
              score: 0.61,
              matched_by: ["vector"],
              content: null,
              resolved: false,
            },
          ],
        }),
        { status: 200 }
      )
    );

    const result = await getMessageSources("conv-1", "msg-2");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.detail.messageId).toBe("msg-2");
      expect(result.detail.content).toBe("You can run Meta ads alongside lead-gen.");
      expect(result.detail.decision).toBe("answer");
      expect(result.detail.confidence).toBe(0.57);
      expect(result.detail.grounded).toBe(true);
      expect(result.detail.sources).toHaveLength(2);
      expect(result.detail.sources[0]).toEqual({
        chunkId: "c1",
        docId: "doc-1",
        score: 0.83,
        matchedBy: ["vector", "keyword"],
        content: "Real chunk text.",
        resolved: true,
      });
      expect(result.detail.sources[1].resolved).toBe(false);
      expect(result.detail.sources[1].content).toBeNull();
      expect(result.detail).not.toHaveProperty("tenant_id");
    }
  });

  it("maps a 200 envelope with an empty sources list to an ok result", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          message_id: "msg-chitchat",
          content: "Hi there!",
          decision: "answer",
          confidence: null,
          grounded: null,
          sources: [],
        }),
        { status: 200 }
      )
    );

    const result = await getMessageSources("conv-1", "msg-chitchat");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.detail.sources).toEqual([]);
    }
  });

  it("maps a 404 MESSAGE_NOT_FOUND to a not-found message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "MESSAGE_NOT_FOUND", message: "x", correlation_id: "c" }),
        { status: 404 }
      )
    );

    const result = await getMessageSources("conv-1", "msg-missing");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/not be found/i);
    }
  });

  it("maps a 403 to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "x", correlation_id: "c" }),
        { status: 403 }
      )
    );

    const result = await getMessageSources("conv-1", "msg-1");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
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

    const result = await getMessageSources("conv-1", "msg-1");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
    }
  });

  it("targets the implicit path when tenantId is omitted, never sending tenant_id", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          message_id: "msg-1",
          content: "hi",
          decision: null,
          confidence: null,
          grounded: null,
          sources: [],
        }),
        { status: 200 }
      )
    );

    await getMessageSources("conv-1", "msg-1");

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/conversations/conv-1/messages/msg-1/sources");
    const body = init?.body ? String(init.body) : "";
    expect(body).not.toContain("tenant_id");
  });

  it("targets the tenant-scoped path when tenantId is provided (PLATFORM_ADMIN)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          message_id: "msg-1",
          content: "hi",
          decision: null,
          confidence: null,
          grounded: null,
          sources: [],
        }),
        { status: 200 }
      )
    );

    await getMessageSources("conv-1", "msg-1", "tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe(
      "http://localhost:8000/admin/tenants/tenant-x/conversations/conv-1/messages/msg-1/sources"
    );
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await getMessageSources("conv-1", "msg-1");
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
          message_id: "msg-1",
          content: "secret reply text",
          decision: "answer",
          confidence: 0.9,
          grounded: true,
          sources: [
            {
              chunk_id: "c1",
              doc_id: "doc-1",
              score: 0.9,
              matched_by: ["vector"],
              content: "secret chunk text",
              resolved: true,
            },
          ],
        }),
        { status: 200 }
      )
    );

    await getMessageSources("conv-1", "msg-1");

    expect(consoleSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });
});
