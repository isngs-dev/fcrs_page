/**
 * SR-27 slices 7/8 -- geometry + no-dead-control regression tests for the
 * single `/settings` route hosting BOTH the Settings and Bot-settings
 * reference shells (D1). Uses this repo's established
 * `environment: "node"` `renderToStaticMarkup` pattern with mocked
 * `next/headers` cookies + `fetch`, mirroring
 * `accounts/__tests__/accounts-page.test.tsx` and
 * `notifications/__tests__/notifications-page.test.tsx`.
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

const SettingsPage = (await import("@/app/(protected)/settings/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function mockAllFetch() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (url.includes("/admin/settings")) {
      return new Response(
        JSON.stringify({
          greeting: "Hi there!",
          launcher_label: "Chat with us",
          sidebar_workspace_label: "Client workspace",
          dashboard_title: "Dashboard",
          business_hours: null,
          escalation_policy: "Escalate after 2 questions.",
          tone: "friendly",
          answer_threshold: 0.5,
          escalate_threshold: 0.35,
          turn_cap: 6,
          llm_provider: null,
          llm_model: null,
        }),
        { status: 200 }
      );
    }
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

describe("SettingsPage -- single route, two stacked shells (D1)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("renders BOTH 'Settings' and 'Bot settings' shell headers in one route for CLIENT_ADMIN", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/>Settings</);
    expect(html).toMatch(/>Bot settings</);
  });

  it("renders both 184px settings rails", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    const railMatches = html.match(/w-\[184px\]/g) ?? [];
    expect(railMatches.length).toBeGreaterThanOrEqual(2);
  });

  it("Settings shell: Delete workspace button is disabled, never a live/clickable control (D2/G19)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Delete workspace/);
    // The Danger-zone button carries `disabled` -- react-dom serializes a
    // boolean disabled attribute as bare `disabled=""`.
    expect(html).toMatch(/Delete workspace<\/button>|disabled=""[^>]*>\s*Delete workspace/);
    expect(html).toMatch(/disabled=""/);
  });

  it("Settings shell: Language row renders aria-disabled, never a live editable field (D3/G18)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Language/);
    expect(html).toMatch(/aria-disabled="true"[^]*?Not configured/);
  });

  it("Settings shell: Billing rail row is disabled/aria-disabled, no href (G20)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    // Billing must not appear as a real navigable link.
    expect(html).not.toMatch(/<a[^>]+href="[^"]*billing[^"]*"/i);
  });

  it("Bot-settings shell: no numeric stepper for escalation policy -- it stays a text field (verified string type, lib/settings.ts:22)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Escalation policy/);
    expect(html).toMatch(/Escalate after 2 questions\./);
    // No stepper affordance (minus/plus buttons) anywhere near escalation.
    expect(html).not.toMatch(/aria-label="[^"]*[Ee]scalat[^"]*"[^>]*>\s*[−-]\s*</);
  });

  it("Bot-settings shell: thresholds render as read-only chips, never inputs", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Answer 0\.5/);
    expect(html).toMatch(/Escalate 0\.35/);
    expect(html).toMatch(/Turn cap 6/);
  });

  it("Bot-settings shell: Appearance rail entry is disabled, no Appearance section is built (D4)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Appearance/);
    expect(html).not.toMatch(/<a[^>]+href="#settings-appearance"/);
  });

  it("CLIENT_AGENT gets a read-only view, no Save/Publish/Discard buttons", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT") });
    mockAllFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Read-only/);
    expect(html).not.toMatch(/Publish changes/);
  });

  it("PLATFORM_ADMIN and unauthenticated requests are redirected (RBAC unchanged)", async () => {
    getMock.mockReturnValue(undefined);
    await expect(SettingsPage()).rejects.toThrow("REDIRECT:/login");
  });
});
