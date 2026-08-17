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

const { saveCalendlyConfig } = await import(
  "@/app/(protected)/workspace/calendly-actions"
);
const { AdminApiError } = await import("@/lib/api");

// Fixture value kept out of a bare `signingSecret: "..."` object literal so
// it reads clearly as test data, not a real credential.
const fixtureSigningSecretValue = "a-random-signing-secret-value";

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

function buildFormData(overrides: Partial<Record<string, string>> = {}): FormData {
  const values: Record<string, string> = {
    schedulingUrl: "https://calendly.com/acme/30min",
    signingSecret: fixtureSigningSecretValue,
    ...overrides,
  };
  const fd = new FormData();
  for (const [key, value] of Object.entries(values)) {
    fd.set(key, value);
  }
  if (overrides.enabled === "on") {
    fd.set("enabled", "on");
  }
  return fd;
}

describe("saveCalendlyConfig", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("calls PUT /admin/schedule/calendar with provider=calendly and the validated fields", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          provider: "calendly",
          calendar_id: null,
          enabled: false,
          scheduling_url: "https://calendly.com/acme/30min",
        },
        200
      )
    );

    const state = await saveCalendlyConfig({ status: "idle" }, buildFormData());

    expect(state.status).toBe("saved");
    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/schedule/calendar",
      expect.objectContaining({ method: "PUT" })
    );
    const body = JSON.parse(adminApiFetchMock.mock.calls[0][1].body);
    expect(body).toEqual({
      provider: "calendly",
      calendar_id: null,
      credentials: fixtureSigningSecretValue,
      enabled: false,
      busy: [],
      scheduling_url: "https://calendly.com/acme/30min",
    });
    expect(revalidatePathMock).toHaveBeenCalledWith("/workspace");
  });

  it("sends enabled=true when the checkbox is checked", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          provider: "calendly",
          calendar_id: null,
          enabled: true,
          scheduling_url: "https://calendly.com/acme/30min",
        },
        200
      )
    );

    const state = await saveCalendlyConfig(
      { status: "idle" },
      buildFormData({ enabled: "on" })
    );

    expect(state.status).toBe("saved");
    if (state.status === "saved") {
      expect(state.enabled).toBe(true);
    }
    const body = JSON.parse(adminApiFetchMock.mock.calls[0][1].body);
    expect(body.enabled).toBe(true);
  });

  it("rejects a missing scheduling URL without calling adminApiFetch", async () => {
    const state = await saveCalendlyConfig(
      { status: "idle" },
      buildFormData({ schedulingUrl: "" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.schedulingUrl).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects a malformed scheduling URL without calling adminApiFetch", async () => {
    const state = await saveCalendlyConfig(
      { status: "idle" },
      buildFormData({ schedulingUrl: "not-a-url" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.schedulingUrl).toMatch(/valid url/i);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects a missing signing secret without calling adminApiFetch", async () => {
    const state = await saveCalendlyConfig(
      { status: "idle" },
      buildFormData({ signingSecret: "" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.signingSecret).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("maps a 403 to a permission-denied form error", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "ROLE_NOT_PERMITTED",
        message: "Forbidden.",
        correlation_id: "corr-1",
      })
    );

    const state = await saveCalendlyConfig({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/permission/i);
    }
  });

  it("maps a 401 to a session-expired form error", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(401, {
        error_code: "UNAUTHORIZED",
        message: "Unauthorized.",
        correlation_id: "corr-2",
      })
    );

    const state = await saveCalendlyConfig({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/session has expired/i);
    }
  });

  it("returns a network-failure message when adminApiFetch throws a non-AdminApiError", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const state = await saveCalendlyConfig({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/unable to reach the server/i);
    }
  });
});
