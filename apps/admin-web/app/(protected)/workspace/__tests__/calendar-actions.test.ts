import { afterEach, describe, expect, it, vi } from "vitest";

const adminApiFetchMock = vi.fn();
const redirectMock = vi.fn((url: string) => {
  throw new Error(`REDIRECT:${url}`);
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (...args: unknown[]) => adminApiFetchMock(...args),
  };
});

vi.mock("next/navigation", () => ({
  redirect: redirectMock,
}));

const { connectGoogleCalendar } = await import(
  "@/app/(protected)/workspace/calendar-actions"
);
const { AdminApiError } = await import("@/lib/api");

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("connectGoogleCalendar", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    redirectMock.mockClear();
  });

  it("calls the authorize endpoint and redirects to the returned URL", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ authorize_url: "https://accounts.google.com/o/oauth2/v2/auth?client_id=x" }, 200)
    );

    await expect(
      connectGoogleCalendar({ status: "idle" }, new FormData())
    ).rejects.toThrow("REDIRECT:https://accounts.google.com/o/oauth2/v2/auth?client_id=x");

    expect(adminApiFetchMock).toHaveBeenCalledWith("/admin/schedule/calendar/google/authorize");
    expect(redirectMock).toHaveBeenCalledWith(
      "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"
    );
  });

  it("maps GOOGLE_OAUTH_NOT_CONFIGURED to a support-facing message, never redirects", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "GOOGLE_OAUTH_NOT_CONFIGURED",
        message: "Google Calendar OAuth is not configured on this deployment.",
        correlation_id: "corr-1",
      })
    );

    const state = await connectGoogleCalendar({ status: "idle" }, new FormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/isn't configured/i);
    }
    expect(redirectMock).not.toHaveBeenCalled();
  });

  it("maps a 403 to a permission-denied message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "ROLE_NOT_PERMITTED",
        message: "Forbidden.",
        correlation_id: "corr-2",
      })
    );

    const state = await connectGoogleCalendar({ status: "idle" }, new FormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/permission/i);
    }
  });

  it("maps a 401 to a session-expired message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(401, {
        error_code: "UNAUTHORIZED",
        message: "Unauthorized.",
        correlation_id: "corr-3",
      })
    );

    const state = await connectGoogleCalendar({ status: "idle" }, new FormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/session has expired/i);
    }
  });

  it("returns a network-failure message when adminApiFetch throws a non-AdminApiError", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const state = await connectGoogleCalendar({ status: "idle" }, new FormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/unable to reach the server/i);
    }
  });
});
