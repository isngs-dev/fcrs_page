/**
 * RBAC + rendering tests for the self-service "Add a chatbot" screen
 * (`/chatbots/new`). Mirrors `clients/new/__tests__/page.test.tsx`'s
 * pattern exactly, adapted for CLIENT_ADMIN-only (not PLATFORM_ADMIN).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import jwt from "jsonwebtoken";

const getMock = vi.fn();
const redirectMock = vi.fn((url: string) => {
  throw new Error(`REDIRECT:${url}`);
});

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

vi.mock("next/navigation", () => ({
  redirect: redirectMock,
}));

const AddOwnChatbotPage = (await import("@/app/(protected)/chatbots/new/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

describe("AddOwnChatbotPage (/chatbots/new)", () => {
  afterEach(() => {
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("renders the 'Add a chatbot' form for CLIENT_ADMIN, with only name/slug fields", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });

    const element = await AddOwnChatbotPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Add a chatbot/);
    expect(html).toMatch(/name="name"/);
    expect(html).toMatch(/name="slug"/);
    // No admin-user fields -- this endpoint creates no user.
    expect(html).not.toMatch(/name="adminEmail"/);
    expect(html).not.toMatch(/name="adminPassword"/);
  });

  it("redirects CLIENT_AGENT to the home shell (not CLIENT_ADMIN-permitted)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT") });

    await expect(AddOwnChatbotPage()).rejects.toThrow("REDIRECT:/");
  });

  it("redirects PLATFORM_ADMIN to the home shell (no account of their own)", async () => {
    getMock.mockReturnValue({ value: signToken("PLATFORM_ADMIN") });

    await expect(AddOwnChatbotPage()).rejects.toThrow("REDIRECT:/");
  });

  it("redirects an unauthenticated request to /login", async () => {
    getMock.mockReturnValue(undefined);

    await expect(AddOwnChatbotPage()).rejects.toThrow("REDIRECT:/login");
  });
});
