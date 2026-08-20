import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const { getBotSettings } = await import("@/lib/settings");

describe("getBotSettings", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 unified body to an ok result with the camelCased shape, nulls preserved", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const body = {
      greeting: "Hi!",
      launcher_label: "Chat with our team",
      sidebar_workspace_label: "Acme support",
      dashboard_title: "Support hub",
      business_hours: { mon: ["09:00", "17:00"] },
      escalation_policy: "Escalate on refunds.",
      tone: "friendly",
      answer_threshold: 0.7,
      escalate_threshold: 0.4,
      turn_cap: 7,
      low_confidence_streak_cap: 5,
      llm_provider: null,
      llm_model: null,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 })
    );

    const result = await getBotSettings();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.settings.greeting).toBe("Hi!");
      expect(result.settings.launcherLabel).toBe("Chat with our team");
      expect(result.settings.sidebarWorkspaceLabel).toBe("Acme support");
      expect(result.settings.dashboardTitle).toBe("Support hub");
      expect(result.settings.businessHours).toEqual({ mon: ["09:00", "17:00"] });
      expect(result.settings.escalationPolicy).toBe("Escalate on refunds.");
      expect(result.settings.tone).toBe("friendly");
      expect(result.settings.answerThreshold).toBe(0.7);
      expect(result.settings.escalateThreshold).toBe(0.4);
      expect(result.settings.turnCap).toBe(7);
      expect(result.settings.lowConfidenceStreakCap).toBe(5);
      // nulls preserved, never coerced to "" / 0
      expect(result.settings.llmProvider).toBeNull();
      expect(result.settings.llmModel).toBeNull();
    }
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "c1" }),
        { status: 403 }
      )
    );

    const result = await getBotSettings();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
      expect(result.correlationId).toBe("c1");
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

    const result = await getBotSettings();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
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

    const result = await getBotSettings();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-xyz");
      expect(result.message).toContain("corr-xyz");
    }
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await getBotSettings();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });

  it("never logs the response body", async () => {
    getMock.mockReturnValue(undefined);
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          greeting: "Secret greeting",
          business_hours: null,
          escalation_policy: null,
          tone: null,
          answer_threshold: 0.7,
          escalate_threshold: 0.4,
          turn_cap: 7,
          low_confidence_streak_cap: 5,
          llm_provider: "anthropic",
          llm_model: "claude",
        }),
        { status: 200 }
      )
    );

    await getBotSettings();

    expect(logSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("targets the implicit /admin/settings path when tenantId is omitted", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          greeting: null,
          business_hours: null,
          escalation_policy: null,
          tone: null,
          answer_threshold: 0.7,
          escalate_threshold: 0.4,
          turn_cap: 7,
          low_confidence_streak_cap: 5,
          llm_provider: null,
          llm_model: null,
        }),
        { status: 200 }
      )
    );

    await getBotSettings();

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/settings");
  });

  it("targets the S12.7 tenant-scoped path when tenantId is provided (PLATFORM_ADMIN)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          greeting: null,
          business_hours: null,
          escalation_policy: null,
          tone: null,
          answer_threshold: 0.7,
          escalate_threshold: 0.4,
          turn_cap: 7,
          low_confidence_streak_cap: 5,
          llm_provider: null,
          llm_model: null,
        }),
        { status: 200 }
      )
    );

    await getBotSettings("tenant-x");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/tenants/tenant-x/settings");
  });
});
