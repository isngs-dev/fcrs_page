/**
 * Unit tests for `deleteChatbotAction` (delete-the-active-chatbot feature).
 * Mirrors `switch-tenant/__tests__/actions.test.ts`'s cookie-bridge pattern
 * exactly, with one difference this action deliberately makes: failures are
 * RETURNED (not redirected) so the confirm dialog can show the real error
 * and stay open, matching `OffRampDialog`'s pessimistic-UI shape.
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

const { deleteChatbotAction } = await import(
  "@/app/(protected)/workspace/delete-chatbot-actions"
);

const STUB_COOKIE_VALUE = ["stub", "session", "value", "for", "tests", "only"].join("-");

function responseWithCookie(value: string): Response {
  const response = new Response(
    JSON.stringify({ deleted_tenant_id: "tenant-1", tenant_id: "tenant-2", name: "Bot Two", slug: "bot-two" }),
    { status: 200 }
  );
  response.headers.append("set-cookie", `access_token=${value}; HttpOnly; Path=/; SameSite=Lax`);
  return response;
}

describe("deleteChatbotAction", () => {
  afterEach(() => {
    adminApiFetchMock.mockReset();
    cookieSetMock.mockReset();
    redirectMock.mockClear();
    revalidatePathMock.mockReset();
  });

  it("calls DELETE /admin/tenants/mine, re-sets the access_token cookie, revalidates, and redirects home", async () => {
    adminApiFetchMock.mockResolvedValue(responseWithCookie(STUB_COOKIE_VALUE));

    await expect(deleteChatbotAction()).rejects.toThrow("REDIRECT:/");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/mine",
      expect.objectContaining({ method: "DELETE" })
    );
    expect(cookieSetMock).toHaveBeenCalledWith(
      "access_token",
      STUB_COOKIE_VALUE,
      expect.objectContaining({ httpOnly: true, path: "/" })
    );
    expect(revalidatePathMock).toHaveBeenCalledWith("/", "layout");
  });

  it("returns an honest error result (never redirects, never sets a cookie) when this is the account's last chatbot", async () => {
    const { AdminApiError } = await import("@/lib/api");
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "LAST_CHATBOT",
        message: "This is the only chatbot left in your account.",
        correlation_id: "corr-1",
      })
    );

    const result = await deleteChatbotAction();

    expect(result).toEqual({
      status: "error",
      message: expect.stringContaining("only chatbot left"),
    });
    expect(cookieSetMock).not.toHaveBeenCalled();
    expect(redirectMock).not.toHaveBeenCalled();
  });

  it("returns an honest error result on a network failure", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await deleteChatbotAction();

    expect(result.status).toBe("error");
    expect(cookieSetMock).not.toHaveBeenCalled();
    expect(redirectMock).not.toHaveBeenCalled();
  });

  it("returns an honest error result when the caller lacks permission (RBAC, defense-in-depth)", async () => {
    const { AdminApiError } = await import("@/lib/api");
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "ROLE_NOT_PERMITTED",
        message: "Not permitted.",
        correlation_id: "corr-2",
      })
    );

    const result = await deleteChatbotAction();

    expect(result).toEqual({
      status: "error",
      message: expect.stringContaining("do not have permission"),
    });
  });
});
