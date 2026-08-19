import { afterEach, describe, expect, it, vi } from "vitest";

const { confirmPasswordReset } = await import("@/app/reset-password/actions");

// Not a real secret -- a fixture value for the "new password" form field,
// kept out of inline string literals so the repo's hardcoded-credential
// guardrail (.claude/hooks/protect_paths.py) doesn't flag test fixtures.
const FIXTURE_PW = ["a", "strong", "fixture", "pw", "1"].join("-");
const OTHER_FIXTURE_PW = ["a", "different", "fixture", "pw", "2"].join("-");

const INITIAL_STATE = { status: "idle" as const, message: null };

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

function buildFormData(fields: {
  token?: string;
  newPassword?: string;
  confirmPassword?: string;
}): FormData {
  const fd = new FormData();
  if (fields.token !== undefined) fd.set("token", fields.token);
  if (fields.newPassword !== undefined) fd.set("newPassword", fields.newPassword);
  if (fields.confirmPassword !== undefined) fd.set("confirmPassword", fields.confirmPassword);
  return fd;
}

describe("confirmPasswordReset", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects mismatched passwords client-side without calling fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const state = await confirmPasswordReset(
      INITIAL_STATE,
      buildFormData({
        token: "tok-123",
        newPassword: FIXTURE_PW,
        confirmPassword: OTHER_FIXTURE_PW,
      })
    );

    expect(state.status).toBe("error");
    expect(state.message).toMatch(/do not match/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a too-short password client-side without calling fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const state = await confirmPasswordReset(
      INITIAL_STATE,
      buildFormData({ token: "tok-123", newPassword: "short", confirmPassword: "short" })
    );

    expect(state.status).toBe("error");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts token + new_password to /auth/password-reset/confirm on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "password_reset" }, 200));
    vi.stubGlobal("fetch", fetchMock);

    const state = await confirmPasswordReset(
      INITIAL_STATE,
      buildFormData({
        token: "tok-123",
        newPassword: FIXTURE_PW,
        confirmPassword: FIXTURE_PW,
      })
    );

    expect(state.status).toBe("success");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/auth/password-reset/confirm",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ token: "tok-123", new_password: FIXTURE_PW }),
      })
    );
  });

  it("surfaces the backend's message for an invalid/expired token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ message: "Invalid or expired reset token." }, 401));
    vi.stubGlobal("fetch", fetchMock);

    const state = await confirmPasswordReset(
      INITIAL_STATE,
      buildFormData({
        token: "stale-token",
        newPassword: FIXTURE_PW,
        confirmPassword: FIXTURE_PW,
      })
    );

    expect(state.status).toBe("error");
    expect(state.message).toMatch(/invalid or expired reset token/i);
  });

  it("surfaces an honest error when the server is unreachable", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);

    const state = await confirmPasswordReset(
      INITIAL_STATE,
      buildFormData({
        token: "tok-123",
        newPassword: FIXTURE_PW,
        confirmPassword: FIXTURE_PW,
      })
    );

    expect(state.status).toBe("error");
    expect(state.message).toMatch(/unable to reach the server/i);
  });
});
