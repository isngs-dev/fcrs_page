import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "./config";
import { RESUME_KEY } from "./resume";
import {
  authHeader,
  getResumeSeed,
  getVisitorSession,
  hydrateFromResume,
  isResumeEnabled,
  mintVisitorSession,
} from "./session";

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
};

function jsonResponse(status: number, body: unknown, extraHeaders?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}

describe("mintVisitorSession", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("stores the token in memory and exposes a working authHeader() on a 200", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { visitor_token: "jwt.abc.def", expires_at: "2026-07-16T12:30:00Z" }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.session.visitorToken).toBe("jwt.abc.def");
    expect(result.session.expiresAt).toBe("2026-07-16T12:30:00Z");

    expect(getVisitorSession()).toEqual(result.session);
    expect(authHeader()).toEqual({ Authorization: "Bearer jwt.abc.def" });
  });

  it("parses an optional tenant launcher_label without altering the admission request", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        visitor_token: "jwt.abc.def",
        expires_at: "2026-07-16T12:30:00Z",
        launcher_label: "Chat with our team",
      }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result).toMatchObject({
      ok: true,
      session: { launcherLabel: "Chat with our team" },
    });
  });

  it("returns a typed invalid-response error, never throws, for an over-long launcher_label", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        visitor_token: "jwt.abc.def",
        expires_at: "2026-07-16T12:30:00Z",
        launcher_label: "x".repeat(41),
      }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result).toMatchObject({ ok: false, error: { errorCode: "INVALID_RESPONSE_SHAPE" } });
  });

  it("sends credentials: 'omit', JSON body { client_key }, and never sends tenant_id", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { visitor_token: "jwt.abc.def", expires_at: "2026-07-16T12:30:00Z" }),
    );

    await mintVisitorSession(baseConfig);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/widget/session");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });

    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).toEqual({ client_key: "pk_test_123" });
    expect(parsedBody).not.toHaveProperty("tenant_id");
  });

  it("returns a typed admission error (no throw, no fake token) on 422 INVALID_CLIENT_KEY", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, {
        error_code: "INVALID_CLIENT_KEY",
        message: "Unknown client key.",
        correlation_id: "corr-1",
      }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_CLIENT_KEY");
    expect(result.error.correlationId).toBe("corr-1");
    expect(result.error.status).toBe(422);
  });

  it("returns a typed admission error on 403 ORIGIN_NOT_ALLOWED", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(403, {
        error_code: "ORIGIN_NOT_ALLOWED",
        message: "Origin not allowed.",
        correlation_id: "corr-2",
      }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("ORIGIN_NOT_ALLOWED");
    expect(result.error.correlationId).toBe("corr-2");
    expect(result.error.status).toBe(403);
  });

  it("returns a typed admission error on 429 rate limit", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(429, {
        error_code: "RATE_LIMITED",
        message: "Too many requests.",
        correlation_id: "corr-3",
      }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.status).toBe(429);
  });

  it("a 429 with a readable Retry-After header yields errorCode RATE_LIMITED and the parsed retryAfterSeconds (S14.6 decision 3)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        429,
        { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-5" },
        { "Retry-After": "12" },
      ),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.retryAfterSeconds).toBe(12);
  });

  it("a 429 WITHOUT a readable Retry-After header yields retryAfterSeconds:null — never a fabricated value (conservative-backoff path)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(429, { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-6" }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.retryAfterSeconds).toBeNull();
  });

  it("a non-429 error carries retryAfterSeconds:null", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, { error_code: "INVALID_CLIENT_KEY", message: "x", correlation_id: "corr-7" }),
    );

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.retryAfterSeconds).toBeNull();
  });

  it("returns a typed NETWORK_ERROR (no throw) when fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
    expect(result.error.status).toBeNull();
  });

  it("returns a typed error when the 200 body fails Zod validation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { visitor_token: "", expires_at: "" }));

    const result = await mintVisitorSession(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });

  it("does not overwrite an existing in-memory session when a later mint attempt fails", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { visitor_token: "jwt.good", expires_at: "2026-07-16T12:30:00Z" }),
    );
    const success = await mintVisitorSession(baseConfig);
    expect(success.ok).toBe(true);
    expect(getVisitorSession()?.visitorToken).toBe("jwt.good");

    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, { error_code: "INVALID_CLIENT_KEY", message: "x", correlation_id: "corr-4" }),
    );
    const failure = await mintVisitorSession(baseConfig);
    expect(failure.ok).toBe(false);

    // A failed re-mint must not fake/clear a previously-good token — no
    // silent fallback in either direction.
    expect(getVisitorSession()?.visitorToken).toBe("jwt.good");
  });

  // =====================================================================
  // SR-3: resume_enabled parsing + initial-record write on a fresh mint
  // =====================================================================

  it("parses resume_enabled:true from the admission response and reports it via isResumeEnabled()", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        visitor_token: "jwt.abc.def",
        expires_at: "2026-07-16T12:30:00Z",
        resume_enabled: true,
      }),
    );

    await mintVisitorSession(baseConfig);

    expect(isResumeEnabled()).toBe(true);
  });

  it("an absent resume_enabled field is treated as false (opt-in default)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { visitor_token: "jwt.abc.def", expires_at: "2026-07-16T12:30:00Z" }),
    );

    await mintVisitorSession(baseConfig);

    expect(isResumeEnabled()).toBe(false);
  });

  it("resume_enabled:false on a successful mint writes NO sessionStorage record (opt-in gate, decision 8)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        visitor_token: "jwt.abc.def",
        expires_at: "2026-07-16T12:30:00Z",
        resume_enabled: false,
      }),
    );

    await mintVisitorSession(baseConfig);

    expect(sessionStorage.getItem(RESUME_KEY)).toBeNull();
  });

  it("resume_enabled:true on a successful mint writes an initial sessionStorage record (token + expiresAt, conversationId:null)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        visitor_token: "jwt.abc.def",
        expires_at: "2026-07-16T12:30:00Z",
        resume_enabled: true,
      }),
    );

    await mintVisitorSession(baseConfig);

    const raw = sessionStorage.getItem(RESUME_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw as string) as Record<string, unknown>;
    expect(parsed.token).toBe("jwt.abc.def");
    expect(parsed.expiresAt).toBe("2026-07-16T12:30:00Z");
    expect(parsed.conversationId).toBeNull();
  });

  it("a failed mint writes no resume record even when a prior resume_enabled was true", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, { error_code: "INVALID_CLIENT_KEY", message: "x", correlation_id: "corr-1" }),
    );

    await mintVisitorSession(baseConfig);

    expect(sessionStorage.getItem(RESUME_KEY)).toBeNull();
  });
});

// =====================================================================
// SR-3: hydrateFromResume -- token reuse, no mint fetch (decision 2)
// =====================================================================

describe("hydrateFromResume", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("sets authHeader() from the stored token and issues NO POST /widget/session (decision 2)", () => {
    hydrateFromResume({
      token: "jwt.resumed",
      expiresAt: "2026-07-16T12:30:00Z",
      conversationId: "conv-abc",
      lastActive: "2026-07-16T12:05:00Z",
    });

    expect(authHeader()).toEqual({ Authorization: "Bearer jwt.resumed" });
    expect(getVisitorSession()).toEqual({ visitorToken: "jwt.resumed", expiresAt: "2026-07-16T12:30:00Z" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exposes the seeded conversationId via getResumeSeed()", () => {
    hydrateFromResume({
      token: "jwt.resumed",
      expiresAt: "2026-07-16T12:30:00Z",
      conversationId: "conv-abc",
      lastActive: "2026-07-16T12:05:00Z",
    });

    expect(getResumeSeed()).toEqual({ conversationId: "conv-abc" });
  });

  it("marks isResumeEnabled() true after a successful hydrate (the record only exists when the tenant opted in)", () => {
    hydrateFromResume({
      token: "jwt.resumed",
      expiresAt: "2026-07-16T12:30:00Z",
      conversationId: null,
      lastActive: "2026-07-16T12:05:00Z",
    });

    expect(isResumeEnabled()).toBe(true);
  });
});
