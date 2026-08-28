import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const { getCallConfig, saveCallConfig } = await import("@/lib/calls");

describe("getCallConfig", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 body to an ok result with the camelCased shape", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          monitored_phone_number: "+15005550006",
          enabled: true,
          text_back_message: "Sorry we missed your call!",
        }),
        { status: 200 }
      )
    );

    const result = await getCallConfig();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.config.monitoredPhoneNumber).toBe("+15005550006");
      expect(result.config.enabled).toBe(true);
      expect(result.config.textBackMessage).toBe("Sorry we missed your call!");
    }
  });

  it("maps the honest unset state (nulls) before first save", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ monitored_phone_number: null, enabled: false, text_back_message: null }),
        { status: 200 }
      )
    );

    const result = await getCallConfig();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.config.monitoredPhoneNumber).toBeNull();
      expect(result.config.enabled).toBe(false);
      expect(result.config.textBackMessage).toBeNull();
    }
  });

  it("maps a 403 to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "c1" }),
        { status: 403 }
      )
    );

    const result = await getCallConfig();
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

    const result = await getCallConfig();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
    }
  });

  it("targets the implicit path when tenantId is omitted", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ monitored_phone_number: null, enabled: false, text_back_message: null }),
        { status: 200 }
      )
    );

    await getCallConfig();

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/calls/config");
  });

  it("targets the tenant-scoped path when tenantId is provided (PLATFORM_ADMIN)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ monitored_phone_number: null, enabled: false, text_back_message: null }),
        { status: 200 }
      )
    );

    await getCallConfig("tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/tenants/tenant-x/calls/config");
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await getCallConfig();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });
});

describe("saveCallConfig", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("PUTs the config as snake_case JSON and maps a 200 response to ok", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          monitored_phone_number: "+15005550006",
          enabled: true,
          text_back_message: "Sorry we missed your call!",
        }),
        { status: 200 }
      )
    );

    const result = await saveCallConfig("+15005550006", true, "Sorry we missed your call!");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.config.monitoredPhoneNumber).toBe("+15005550006");
    }
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/calls/config");
    expect(init?.method).toBe("PUT");
    expect(init?.body).toBe(
      JSON.stringify({
        monitored_phone_number: "+15005550006",
        enabled: true,
        text_back_message: "Sorry we missed your call!",
      })
    );
  });

  it("targets the tenant-scoped path when tenantId is provided (PLATFORM_ADMIN)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ monitored_phone_number: "+15005550006", enabled: true, text_back_message: "Hi" }),
        { status: 200 }
      )
    );

    await saveCallConfig("+15005550006", true, "Hi", "tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/tenants/tenant-x/calls/config");
  });

  it("maps a 422 to a generic error result with the correlation id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "VALIDATION_ERROR", message: "bad phone", correlation_id: "corr-1" }),
        { status: 422 }
      )
    );

    const result = await saveCallConfig("not-a-phone", true, "Hi");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-1");
    }
  });

  it("maps a 403 to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "c1" }),
        { status: 403 }
      )
    );

    const result = await saveCallConfig("+15005550006", true, "Hi");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
    }
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await saveCallConfig("+15005550006", true, "Hi");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });
});
