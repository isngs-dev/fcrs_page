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
};

function jsonResponse(status: number, body: unknown, extraHeaders?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}

describe("submitIdentity", () => {
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

  it("CHAT_IDENTITY_CONSENT_PURPOSE/_TEXT are distinct from lead.ts's CONSENT_PURPOSE/CONSENT_TEXT", async () => {
    const { CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import("./identity");
    const { CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    expect(CHAT_IDENTITY_CONSENT_PURPOSE).not.toBe(CONSENT_PURPOSE);
    expect(CHAT_IDENTITY_CONSENT_TEXT).not.toBe(CONSENT_TEXT);
    expect(CHAT_IDENTITY_CONSENT_PURPOSE).toBe("chat_identification");
  });

  it("returns a typed outcome on a mocked 201 { lead_id, status }", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { lead_id: "lead-1", status: "new" }));
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    const result = await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.identity.leadId).toBe("lead-1");
    expect(result.identity.status).toBe("new");
  });

  it("posts to /public/chat/identity with name/email/consent, Authorization: Bearer, credentials: omit, and never a tenant_id/visitor_id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { lead_id: "lead-1", status: "new" }));
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/chat/identity");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer jwt.abc.def",
    });

    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).toEqual({
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });
    expect(parsedBody).not.toHaveProperty("tenant_id");
    expect(parsedBody).not.toHaveProperty("visitor_id");
  });

  it("returns a typed IdentityError on a mocked 422 CONSENT_REQUIRED (no throw, no fabricated success)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, {
        error_code: "CONSENT_REQUIRED",
        message: "Consent to store contact information is required.",
        correlation_id: "corr-1",
      }),
    );
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    const result = await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("CONSENT_REQUIRED");
    expect(result.error.correlationId).toBe("corr-1");
    expect(result.error.status).toBe(422);
  });

  it("returns a typed IdentityError on a mocked 401", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { error_code: "AUTHENTICATION_ERROR", message: "Invalid token.", correlation_id: "corr-3" }),
    );
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    const result = await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("AUTHENTICATION_ERROR");
    expect(result.error.status).toBe(401);
  });

  it("returns a typed INVALID_RESPONSE_SHAPE error when the 201 body fails Zod validation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { status: "new" }));
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    const result = await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });

  it("returns a typed NETWORK_ERROR (no throw) when fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    const result = await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
    expect(result.error.status).toBeNull();
  });

  it("returns a typed NO_SESSION error and issues no fetch when authHeader() is null", async () => {
    authHeaderMock.mockReturnValue(null);
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    const result = await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NO_SESSION");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a 429 with a readable Retry-After header yields errorCode RATE_LIMITED and the parsed retryAfterSeconds", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        429,
        { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-5" },
        { "Retry-After": "20" },
      ),
    );
    const { submitIdentity, CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } = await import(
      "./identity"
    );

    const result = await submitIdentity(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.retryAfterSeconds).toBe(20);
  });
});
