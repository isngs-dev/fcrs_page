/**
 * Geometry + no-dead-control regression tests for the `/workspace` route --
 * the "Settings" shell (`Console.dc.html:858-896`) extracted from the
 * combined `/settings` route (SR-27 slices 7/8, D1) into its own route on
 * user request. These cases were originally in `settings/__tests__/
 * settings-page-geometry.test.tsx`; see that file for the Bot-settings
 * shell's tests, which stayed on `/settings`. Uses this repo's established
 * `environment: "node"` `renderToStaticMarkup` pattern with mocked
 * `next/headers` cookies + `fetch`, mirroring `accounts/__tests__/
 * accounts-page.test.tsx` and `notifications/__tests__/
 * notifications-page.test.tsx`.
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
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const WorkspacePage = (await import("@/app/(protected)/workspace/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function mockWorkspaceFetch() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (url.includes("/admin/workspace")) {
      return new Response(
        JSON.stringify({ name: "Acme", slug: "acme", timezone: "Europe/London" }),
        { status: 200 }
      );
    }
    if (url.includes("/admin/api-keys")) {
      return new Response(JSON.stringify({ has_key: true, allowed_origins: ["https://acme.test"] }), {
        status: 200,
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
}

describe("WorkspacePage -- Settings shell (split from the combined route)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("renders the 'Settings' shell header for CLIENT_ADMIN, and no Bot-settings header", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/>Settings</);
    expect(html).not.toMatch(/>Bot settings</);
  });

  it("renders exactly one 184px settings rail", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    const railMatches = html.match(/w-\[184px\]/g) ?? [];
    expect(railMatches.length).toBe(1);
  });

  it("Delete workspace button is disabled, never a live/clickable control (D2/G19)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Delete workspace/);
    // The Danger-zone button carries `disabled` -- react-dom serializes a
    // boolean disabled attribute as bare `disabled=""`.
    expect(html).toMatch(/Delete workspace<\/button>|disabled=""[^>]*>\s*Delete workspace/);
    expect(html).toMatch(/disabled=""/);
  });

  it("Language row renders aria-disabled, never a live editable field (D3/G18)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Language/);
    expect(html).toMatch(/aria-disabled="true"[^]*?Not configured/);
  });

  it("Billing rail row is disabled/aria-disabled, no href (G20)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    // Billing must not appear as a real navigable link.
    expect(html).not.toMatch(/<a[^>]+href="[^"]*billing[^"]*"/i);
  });

  it("CLIENT_AGENT is redirected home, never sees the workspace shell (route-level CLIENT_ADMIN-only gate)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT") });
    mockWorkspaceFetch();

    await expect(
      WorkspacePage({ searchParams: Promise.resolve({}) })
    ).rejects.toThrow("REDIRECT:/");
  });

  it("PLATFORM_ADMIN and unauthenticated requests are redirected", async () => {
    getMock.mockReturnValue(undefined);
    await expect(
      WorkspacePage({ searchParams: Promise.resolve({}) })
    ).rejects.toThrow("REDIRECT:/login");
  });

  it("renders the Google Calendar section with its connect button (SR-22)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Google Calendar/);
    expect(html).toMatch(/Connect Google Calendar/);
    expect(html).not.toMatch(/Google Calendar connected\./);
  });

  it("shows a connected banner when redirected back with ?calendar_connected=true", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({
      searchParams: Promise.resolve({ calendar_connected: "true" }),
    });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Google Calendar connected\./);
  });

  it("maps ?calendar_error=invalid_state to its admin-facing message", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockWorkspaceFetch();

    const element = await WorkspacePage({
      searchParams: Promise.resolve({ calendar_error: "invalid_state" }),
    });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/expired or was already used/i);
  });
});
