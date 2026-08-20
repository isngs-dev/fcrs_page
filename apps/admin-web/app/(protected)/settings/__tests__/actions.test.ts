import { afterEach, describe, expect, it, vi } from "vitest";

const adminApiFetchMock = vi.fn();
const revalidatePathMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (...args: unknown[]) => adminApiFetchMock(...args),
  };
});

vi.mock("next/cache", () => ({
  revalidatePath: (...args: unknown[]) => revalidatePathMock(...args),
}));

const { saveSettings } = await import("@/app/(protected)/settings/actions");
const { AdminApiError } = await import("@/lib/api");

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

function buildFormData(overrides: Partial<Record<string, string>> = {}): FormData {
  const values: Record<string, string> = {
    greeting: "Hi there!",
    launcherLabel: "Chat with our team",
    sidebarWorkspaceLabel: "Acme support",
    dashboardTitle: "Support hub",
    escalationPolicy: "Escalate on refunds.",
    tone: "friendly",
    businessHoursText: "",
    ...overrides,
  };
  const fd = new FormData();
  for (const [key, value] of Object.entries(values)) {
    fd.set(key, value);
  }
  return fd;
}

describe("saveSettings", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("returns the PUT response body's values as the saved state, NOT the submitted form values (confirmed, not optimistic)", async () => {
    // The submitted greeting differs from what the "backend" returns, to
    // prove the returned state reflects the response body.
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          greeting: "Server-normalized greeting",
          business_hours: null,
          escalation_policy: "Escalate on refunds.",
          tone: "friendly",
          answer_threshold: 0.7,
          escalate_threshold: 0.4,
          turn_cap: 7,
          low_confidence_streak_cap: 7,
          llm_provider: null,
          llm_model: null,
        },
        200
      )
    );

    const state = await saveSettings(
      undefined,
      { status: "idle" },
      buildFormData({ greeting: "What the user typed" })
    );

    expect(state.status).toBe("saved");
    if (state.status === "saved") {
      expect(state.settings.greeting).toBe("Server-normalized greeting");
      expect(state.settings.greeting).not.toBe("What the user typed");
    }
    expect(JSON.parse(adminApiFetchMock.mock.calls[0][1].body)).toMatchObject({
      launcher_label: "Chat with our team",
      sidebar_workspace_label: "Acme support",
      dashboard_title: "Support hub",
    });
  });

  it("rejects an invalid business_hours textarea without calling adminApiFetch", async () => {
    const state = await saveSettings(
      undefined,
      { status: "idle" },
      buildFormData({ businessHoursText: "{" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.businessHoursText).toMatch(/valid json object/i);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects a business_hours array without calling adminApiFetch", async () => {
    const state = await saveSettings(
      undefined,
      { status: "idle" },
      buildFormData({ businessHoursText: "[1,2]" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.businessHoursText).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects an over-length greeting client-side without calling adminApiFetch", async () => {
    const state = await saveSettings(
      undefined,
      { status: "idle" },
      buildFormData({ greeting: "a".repeat(2001) })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.greeting).toMatch(/2000/);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects an over-length tone client-side without calling adminApiFetch", async () => {
    const state = await saveSettings(
      undefined,
      { status: "idle" },
      buildFormData({ tone: "a".repeat(101) })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.tone).toMatch(/100/);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects an over-length launcher label client-side without calling adminApiFetch", async () => {
    const state = await saveSettings(
      undefined,
      { status: "idle" },
      buildFormData({ launcherLabel: "a".repeat(41) })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.launcherLabel).toMatch(/40/);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects over-length workspace labels client-side without calling adminApiFetch", async () => {
    const state = await saveSettings(
      undefined,
      { status: "idle" },
      buildFormData({ sidebarWorkspaceLabel: "a".repeat(81) })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.sidebarWorkspaceLabel).toMatch(/80/);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a permission-denied form error", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "ROLE_NOT_PERMITTED",
        message: "Forbidden.",
        correlation_id: "corr-1",
      })
    );

    const state = await saveSettings(undefined, { status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/permission/i);
    }
  });

  it("maps a 401 to a session-expired message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(401, {
        error_code: "AUTHENTICATION_ERROR",
        message: "x",
        correlation_id: "corr-2",
      })
    );

    const state = await saveSettings(undefined, { status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/session/i);
    }
  });

  it("maps a 422 (empty error envelope, FastAPI default shape) to an honest generic form message", async () => {
    // Simulates the documented FastAPI-default {detail:[...]} shape --
    // errorCode/message come back empty/undefined per the Investigation.
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "",
        message: "",
        correlation_id: "corr-3",
      })
    );

    const state = await saveSettings(undefined, { status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/rejected one or more values/i);
    }
  });

  it("maps an unknown error to a generic message including the correlation id", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(500, {
        error_code: "INTERNAL_SERVER_ERROR",
        message: "boom",
        correlation_id: "corr-unknown-xyz",
      })
    );

    const state = await saveSettings(undefined, { status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toContain("corr-unknown-xyz");
    }
  });

  it("returns a network-failure message when adminApiFetch throws a non-AdminApiError", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const state = await saveSettings(undefined, { status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/unable to reach the server/i);
    }
  });

  it("calls revalidatePath('/settings') on a successful save", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          greeting: null,
          business_hours: null,
          escalation_policy: null,
          tone: null,
          answer_threshold: 0.7,
          escalate_threshold: 0.4,
          turn_cap: 7,
          low_confidence_streak_cap: 7,
          llm_provider: null,
          llm_model: null,
        },
        200
      )
    );

    await saveSettings(undefined, { status: "idle" }, buildFormData());

    expect(revalidatePathMock).toHaveBeenCalledWith("/settings");
  });

  it("does not call revalidatePath on a client-side validation failure", async () => {
    await saveSettings(undefined, { status: "idle" }, buildFormData({ businessHoursText: "{" }));

    expect(revalidatePathMock).not.toHaveBeenCalled();
  });

  it("never logs the response body to the console", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          greeting: "A secret internal greeting fixture",
          business_hours: null,
          escalation_policy: null,
          tone: null,
          answer_threshold: 0.7,
          escalate_threshold: 0.4,
          turn_cap: 7,
          low_confidence_streak_cap: 7,
          llm_provider: null,
          llm_model: null,
        },
        200
      )
    );

    await saveSettings(undefined, { status: "idle" }, buildFormData());

    for (const spy of [logSpy, errorSpy, warnSpy]) {
      for (const call of spy.mock.calls) {
        expect(call.join(" ")).not.toContain("A secret internal greeting fixture");
      }
    }
  });

  it("targets the S12.7 tenant-scoped PUT path and revalidates the client screen when tenantId is bound", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          greeting: "Hi there!",
          business_hours: null,
          escalation_policy: "Escalate on refunds.",
          tone: "friendly",
          answer_threshold: 0.7,
          escalate_threshold: 0.4,
          turn_cap: 7,
          low_confidence_streak_cap: 7,
          llm_provider: null,
          llm_model: null,
        },
        200
      )
    );

    await saveSettings("tenant-x", { status: "idle" }, buildFormData());

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/settings",
      expect.objectContaining({ method: "PUT" })
    );
    expect(revalidatePathMock).toHaveBeenCalledWith("/clients/tenant-x/settings");
  });
});
