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

describe("submitLead", () => {
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

  it("returns a typed Lead on a mocked 201 { lead_id, status }", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { lead_id: "lead-1", status: "new" }));
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.lead.leadId).toBe("lead-1");
    expect(result.lead.status).toBe("new");
  });

  it("sends Authorization: Bearer, credentials: omit, JSON body with name/email/phone/source:widget and a consent{granted,purpose,text} whose text equals the exported TEXT, and never a tenant_id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { lead_id: "lead-1", status: "new" }));
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      phone: "555-1234",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/leads");
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
      phone: "555-1234",
      source: "widget",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });
    expect((parsedBody.consent as { text: string }).text).toBe(CONSENT_TEXT);
    expect(parsedBody).not.toHaveProperty("tenant_id");
  });

  it("omits phone from the body when not provided", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { lead_id: "lead-1", status: "new" }));
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).not.toHaveProperty("phone");
  });

  it("returns a typed LeadError on a mocked 422 CONSENT_REQUIRED (no throw, no fabricated success)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, {
        error_code: "CONSENT_REQUIRED",
        message: "Consent to store contact information is required.",
        correlation_id: "corr-1",
      }),
    );
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("CONSENT_REQUIRED");
    expect(result.error.correlationId).toBe("corr-1");
    expect(result.error.status).toBe(422);
  });

  it("returns a typed LeadError on a mocked 422 validation error", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, {
        error_code: "VALIDATION_ERROR",
        message: "email must contain @",
        correlation_id: "corr-2",
      }),
    );
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "not-an-email",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("VALIDATION_ERROR");
    expect(result.error.status).toBe(422);
  });

  it("returns a typed LeadError on a mocked 401", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { error_code: "AUTHENTICATION_ERROR", message: "Invalid token.", correlation_id: "corr-3" }),
    );
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("AUTHENTICATION_ERROR");
    expect(result.error.status).toBe(401);
  });

  it("returns a typed INVALID_RESPONSE_SHAPE error when the 201 body fails Zod validation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { status: "new" }));
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });

  it("returns a typed NETWORK_ERROR (no throw) when fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
    expect(result.error.status).toBeNull();
  });

  it("returns a typed NO_SESSION error and issues no fetch when authHeader() is null", async () => {
    authHeaderMock.mockReturnValue(null);
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NO_SESSION");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a 429 with a readable Retry-After header yields errorCode RATE_LIMITED and the parsed retryAfterSeconds (S14.6 decision 3)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        429,
        { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-5" },
        { "Retry-After": "20" },
      ),
    );
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.retryAfterSeconds).toBe(20);
  });

  it("a 429 WITHOUT a readable Retry-After header yields retryAfterSeconds:null", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(429, { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-6" }),
    );
    const { submitLead, CONSENT_PURPOSE, CONSENT_TEXT } = await import("./lead");

    const result = await submitLead(baseConfig, {
      name: "Ada Lovelace",
      email: "ada@example.com",
      consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.retryAfterSeconds).toBeNull();
  });
});
