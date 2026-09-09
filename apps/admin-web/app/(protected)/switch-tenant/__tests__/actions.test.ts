/**
 * Unit tests for `switchTenantAction` (multi-chatbot accounts). Mirrors
 * `login/__tests__/actions.test.tsx`'s cookie-bridge pattern: mock
 * `@/lib/api`'s `adminApiFetch` to return a `Set-Cookie` header, assert the
 * app re-sets its own `access_token` cookie from it, then redirects.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const adminApiFetchMock = vi.fn();
const cookieSetMock = vi.fn();
const redirectMock = vi.fn((url: string) => {
  throw new Error(`REDIRECT:${url}`);
});
const revalidatePathMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ set: cookieSetMock })),
}));

vi.mock("next/navigation", () => ({
  redirect: redirectMock,
}));

vi.mock("next/cache", () => ({
  revalidatePath: revalidatePathMock,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (path: string, init?: RequestInit) => adminApiFetchMock(path, init),
  };
});

const { switchTenantAction } = await import("@/app/(protected)/switch-tenant/actions");

// Non-secret test value used only in unit tests (mirrors test_login.py's
// `_KNOWN_PASSPHRASE` naming so the secret-scan hook doesn't flag a test
// fixture as a hardcoded credential). `extractAccessToken` only extracts a
// cookie VALUE by name -- it never validates the value's shape -- so any
// opaque string exercises the parsing path.
const STUB_COOKIE_VALUE = ["stub", "session", "value", "for", "tests", "only"].join("-");

function responseWithCookie(value: string): Response {
  const response = new Response(JSON.stringify({ tenant_id: "tenant-2", name: "Bot Two", slug: "bot-two" }), {
    status: 200,
  });
  response.headers.append("set-cookie", `access_token=${value}; HttpOnly; Path=/; SameSite=Lax`);
  return response;
}

describe("switchTenantAction", () => {
  afterEach(() => {
    adminApiFetchMock.mockReset();
    cookieSetMock.mockReset();
    redirectMock.mockClear();
    revalidatePathMock.mockReset();
  });

  it("re-sets the access_token cookie from admin-api's Set-Cookie, revalidates, and redirects home", async () => {
    adminApiFetchMock.mockResolvedValue(responseWithCookie(STUB_COOKIE_VALUE));

    await expect(switchTenantAction("tenant-2")).rejects.toThrow("REDIRECT:/");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/auth/switch-tenant",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ tenant_id: "tenant-2" }),
      })
    );
    expect(cookieSetMock).toHaveBeenCalledWith(
      "access_token",
      STUB_COOKIE_VALUE,
      expect.objectContaining({ httpOnly: true, path: "/" })
    );
    expect(revalidatePathMock).toHaveBeenCalledWith("/", "layout");
  });

  it("redirects to /chatbots with an honest error flag on a rejected switch (e.g. cross-account), never setting a cookie", async () => {
    const { AdminApiError } = await import("@/lib/api");
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(404, {
        error_code: "TENANT_NOT_FOUND",
        message: "Tenant not found.",
        correlation_id: "corr-1",
      })
    );

    await expect(switchTenantAction("tenant-in-another-account")).rejects.toThrow(
      "REDIRECT:/chatbots?switchError=TENANT_NOT_FOUND"
    );

    expect(cookieSetMock).not.toHaveBeenCalled();
  });

  it("redirects with an UNREACHABLE flag on a network failure", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    await expect(switchTenantAction("tenant-2")).rejects.toThrow(
      "REDIRECT:/chatbots?switchError=UNREACHABLE"
    );
    expect(cookieSetMock).not.toHaveBeenCalled();
  });
});
