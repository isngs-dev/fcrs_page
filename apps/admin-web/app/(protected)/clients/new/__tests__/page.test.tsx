/**
 * RBAC + rendering tests for the dedicated "Add a chatbot" screen
 * (`/clients/new`). Mirrors the `environment: "node"` + mocked
 * `next/headers`/`next/navigation` pattern established by
 * `settings/__tests__/settings-page-geometry.test.tsx`.
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

const AddChatbotPage = (await import("@/app/(protected)/clients/new/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

describe("AddChatbotPage (/clients/new)", () => {
  afterEach(() => {
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("renders the 'Add a chatbot' form for PLATFORM_ADMIN", async () => {
    getMock.mockReturnValue({ value: signToken("PLATFORM_ADMIN") });

    const element = await AddChatbotPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Add a chatbot/);
    expect(html).toMatch(/name="name"/);
    expect(html).toMatch(/name="slug"/);
    expect(html).toMatch(/name="adminEmail"/);
  });

  it("redirects CLIENT_ADMIN to the home shell", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });

    await expect(AddChatbotPage()).rejects.toThrow("REDIRECT:/");
  });

  it("redirects CLIENT_AGENT to the home shell", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT") });

    await expect(AddChatbotPage()).rejects.toThrow("REDIRECT:/");
  });

  it("redirects an unauthenticated request to /login", async () => {
    getMock.mockReturnValue(undefined);

    await expect(AddChatbotPage()).rejects.toThrow("REDIRECT:/login");
  });
});
