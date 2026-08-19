import { afterEach, describe, expect, it, vi } from "vitest";

const { requestPasswordReset } = await import("@/app/forgot-password/actions");

const INITIAL_STATE = { status: "idle" as const, message: null };

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

function buildFormData(email: string | undefined): FormData {
  const fd = new FormData();
  if (email !== undefined) fd.set("email", email);
  return fd;
}

describe("requestPasswordReset", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects an invalid email client-side without calling fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const state = await requestPasswordReset(INITIAL_STATE, buildFormData("not-an-email"));

    expect(state.status).toBe("error");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts to /auth/password-reset/request and shows the generic message on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "reset_requested" }, 200));
    vi.stubGlobal("fetch", fetchMock);

    const state = await requestPasswordReset(
      INITIAL_STATE,
      buildFormData("admin@fcrs-demo.local")
    );

    expect(state.status).toBe("submitted");
    expect(state.message).toMatch(/if that email is registered/i);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/auth/password-reset/request",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("shows the same generic message even for an unregistered email (enumeration-safe)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "reset_requested" }, 200));
    vi.stubGlobal("fetch", fetchMock);

    const state = await requestPasswordReset(
      INITIAL_STATE,
      buildFormData("nobody@example.com")
    );

    expect(state.status).toBe("submitted");
    expect(state.message).toMatch(/if that email is registered/i);
  });

  it("surfaces an honest error when the server is unreachable", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);

    const state = await requestPasswordReset(
      INITIAL_STATE,
      buildFormData("admin@fcrs-demo.local")
    );

    expect(state.status).toBe("error");
    expect(state.message).toMatch(/unable to reach the server/i);
  });

  it("surfaces an honest error on a non-2xx response (e.g. rate limited)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ message: "Too many requests." }, 429));
    vi.stubGlobal("fetch", fetchMock);

    const state = await requestPasswordReset(
      INITIAL_STATE,
      buildFormData("admin@fcrs-demo.local")
    );

    expect(state.status).toBe("error");
  });
});
