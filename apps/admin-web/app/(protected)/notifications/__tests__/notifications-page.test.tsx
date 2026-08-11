/**
 * SR-27 slice 1 -- geometry-regression tests for Notifications
 * (`Console.dc.html:430-457`), using this repo's established
 * `environment: "node"` `renderToStaticMarkup` pattern with a mocked
 * `next/headers` cookie + `fetch`, mirroring `accounts/__tests__/accounts-page.test.tsx`.
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
  useRouter: () => ({ push: vi.fn() }),
}));

const NotificationsPage = (await import("@/app/(protected)/notifications/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function emptyListResponse() {
  return new Response(JSON.stringify({ items: [], total: 0, unread_count: 0, limit: 50, offset: 0 }), {
    status: 200,
  });
}

describe("NotificationsPage geometry (SR-27 slice 1)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("renders the 28px/600 title", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await NotificationsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("text-[28px]");
    expect(html).toContain("font-semibold");
  });

  it("renders exactly 3 filter pills -- All / Leads / System, never a 4th Mentions pill (D11)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await NotificationsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/>All</);
    expect(html).toMatch(/>Leads</);
    expect(html).toMatch(/>System</);
    expect(html).not.toMatch(/Mentions/i);
  });

  it("renders the empty state with a 64px (h-16 w-16) circle, real SVG bell, no emoji", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await NotificationsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("h-16");
    expect(html).toContain("w-16");
    expect(html).toContain("<svg");
    expect(html).not.toContain("\u{1F514}"); // no bell emoji
  });

  it("renders the empty title at 17px/600 and body max-width 380px", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyListResponse());

    const element = await NotificationsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).toContain("text-[17px]");
    expect(html).toContain("max-w-[380px]");
  });

  it("never renders a hardcoded #fdfdec fallback -- uses the design token", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              event_id: "evt-1",
              kind: "lead_captured",
              category: "leads",
              target_type: null,
              target_id: null,
              payload: null,
              actor_id: null,
              created_at: "2026-08-01T00:00:00Z",
              read: false,
            },
          ],
          total: 1,
          unread_count: 1,
          limit: 50,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    const element = await NotificationsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toContain("fdfdec");
  });
});
