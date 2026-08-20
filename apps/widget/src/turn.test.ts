import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "./config";

const authHeaderMock = vi.fn<() => { Authorization: string } | null>();

vi.mock("./session", () => ({
  authHeader: () => authHeaderMock(),
}));

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
  position: "right",
};

function jsonResponse(status: number, body: unknown, extraHeaders?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}

const sampleResponseBody = {
  conversation_id: "conv-1",
  message_id: "msg-1",
  reply: "Hello, how can I help?",
  decision: "answer",
  confidence: 0.9,
  sources: [{ doc_id: "d1", chunk_id: "c1", score: 0.8, matched_by: ["vector"] }],
  action: null,
};

describe("sendTurn", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    authHeaderMock.mockReset();
    authHeaderMock.mockReturnValue({ Authorization: "Bearer jwt.abc.def" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns a typed Turn on a mocked 200 ChatMessageResponse", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sampleResponseBody));
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.turn.reply).toBe("Hello, how can I help?");
    expect(result.turn.decision).toBe("answer");
    expect(result.turn.action).toBeNull();
    expect(result.turn.conversationId).toBe("conv-1");
  });

  it("sends Authorization: Bearer from authHeader(), credentials: omit, JSON body { message, conversation_id }, and never a tenant_id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sampleResponseBody));
    const { sendTurn } = await import("./turn");

    await sendTurn(baseConfig, { message: "hi", conversationId: "conv-prior" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/chat/message");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer jwt.abc.def",
    });

    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).toEqual({ message: "hi", conversation_id: "conv-prior" });
    expect(parsedBody).not.toHaveProperty("tenant_id");
  });

  it("returns a typed TurnError (no throw, no fabricated reply) on a 422 LLM_NOT_CONFIGURED", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, {
        error_code: "LLM_NOT_CONFIGURED",
        message: "No LLM provider configured.",
        correlation_id: "corr-1",
      }),
    );
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("LLM_NOT_CONFIGURED");
    expect(result.error.correlationId).toBe("corr-1");
    expect(result.error.status).toBe(422);
  });

  it("returns a typed TurnError on a 502 LLM_ERROR", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(502, { error_code: "LLM_ERROR", message: "Provider failed.", correlation_id: "corr-2" }),
    );
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("LLM_ERROR");
    expect(result.error.status).toBe(502);
  });

  it("returns a typed TurnError on a 404 CONVERSATION_NOT_FOUND", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(404, {
        error_code: "CONVERSATION_NOT_FOUND",
        message: "No such conversation.",
        correlation_id: "corr-3",
      }),
    );
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: "stale-conv" });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("CONVERSATION_NOT_FOUND");
    expect(result.error.status).toBe(404);
  });

  it("returns a typed TurnError on a 401 (expired/non-visitor session)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { error_code: "AUTHENTICATION_ERROR", message: "Invalid token.", correlation_id: "corr-4" }),
    );
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.status).toBe(401);
  });

  it("returns a typed INVALID_RESPONSE_SHAPE error when the 200 body fails Zod validation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { reply: "missing required fields" }));
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });

  it("accepts decision=identity_gate and action=identity_form (SR-14) without INVALID_RESPONSE_SHAPE", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        ...sampleResponseBody,
        decision: "identity_gate",
        confidence: null,
        sources: [],
        action: "identity_form",
      }),
    );
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.turn.decision).toBe("identity_gate");
    expect(result.turn.action).toBe("identity_form");
  });

  it("returns a typed NETWORK_ERROR (no throw) when fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
    expect(result.error.status).toBeNull();
  });

  it("returns a typed NO_SESSION error and issues no fetch when authHeader() is null", async () => {
    authHeaderMock.mockReturnValue(null);
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NO_SESSION");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a 429 with a readable Retry-After header yields errorCode RATE_LIMITED and the parsed retryAfterSeconds (S14.6 decision 3)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        429,
        { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-9" },
        { "Retry-After": "7" },
      ),
    );
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.retryAfterSeconds).toBe(7);
  });

  it("a 429 WITHOUT a readable Retry-After header yields retryAfterSeconds:null — the cross-origin-unreadable / conservative-backoff path", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(429, { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-10" }),
    );
    const { sendTurn } = await import("./turn");

    const result = await sendTurn(baseConfig, { message: "hi", conversationId: null });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.retryAfterSeconds).toBeNull();
  });

  it("the request shape is unchanged by the retryAfterSeconds addition: Bearer present, no tenant_id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sampleResponseBody));
    const { sendTurn } = await import("./turn");

    await sendTurn(baseConfig, { message: "hi", conversationId: null });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer jwt.abc.def");
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).not.toHaveProperty("tenant_id");
  });
});
