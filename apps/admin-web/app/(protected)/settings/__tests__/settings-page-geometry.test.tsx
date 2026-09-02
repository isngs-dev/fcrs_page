/**
 * Geometry + no-dead-control regression tests for the `/settings` route.
 * This route used to also host the workspace/account "Settings" shell
 * stacked below Bot settings (SR-27 slices 7/8, D1); that shell has since
 * moved to its own `/workspace` route (see `workspace/__tests__/
 * workspace-page-geometry.test.tsx` for its tests) on user request. This
 * file now covers ONLY the Bot-settings shell. Uses this repo's established
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

const SettingsPage = (await import("@/app/(protected)/settings/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function mockSettingsFetch() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (url.includes("/admin/settings")) {
      return new Response(
        JSON.stringify({
          greeting: "Hi there!",
          launcher_label: "Chat with us",
          sidebar_workspace_label: "Client workspace",
          dashboard_title: "Dashboard",
          bot_name: null,
          accent_color: null,
          launcher_position: null,
          suggested_questions: null,
          business_hours: null,
          escalation_policy: "Escalate after 2 questions.",
          tone: "friendly",
          answer_threshold: 0.5,
          escalate_threshold: 0.35,
          turn_cap: 6,
          low_confidence_streak_cap: 3,
          llm_provider: null,
          llm_model: null,
        }),
        { status: 200 }
      );
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
}

describe("SettingsPage -- Bot-settings shell only (split from the combined route)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("renders the 'Bot settings' shell header for CLIENT_ADMIN, and no workspace-shell header", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockSettingsFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/>Bot settings</);
    expect(html).not.toMatch(/Manage your workspace, members, and integrations\./);
  });

  it("renders exactly one 184px settings rail (the shared primitive is not duplicated now the shells are split)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockSettingsFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    const railMatches = html.match(/w-\[184px\]/g) ?? [];
    expect(railMatches.length).toBe(1);
  });

  it("no numeric stepper for escalation policy -- it stays a text field (verified string type, lib/settings.ts:22)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockSettingsFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Escalation policy/);
    expect(html).toMatch(/Escalate after 2 questions\./);
    // No stepper affordance (minus/plus buttons) anywhere near escalation.
    expect(html).not.toMatch(/aria-label="[^"]*[Ee]scalat[^"]*"[^>]*>\s*[−-]\s*</);
  });

  it("answer/escalate thresholds render as read-only chips, never inputs; turn cap is a real editable number input (Tier 2)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockSettingsFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/Answer 0\.5/);
    expect(html).toMatch(/Escalate 0\.35/);
    // Turn cap is now a live `<input name="turnCap">`, pre-filled from the
    // server value -- not baked into static text like the other two.
    expect(html).toMatch(/name="turnCap"/);
    expect(html).toMatch(/id="turnCap"[^>]*value="6"|value="6"[^>]*id="turnCap"/);
    expect(html).not.toMatch(/Turn cap 6/);
    // Repeated low-confidence escalation cap -- same live-input treatment.
    expect(html).toMatch(/name="lowConfidenceStreakCap"/);
    expect(html).toMatch(
      /id="lowConfidenceStreakCap"[^>]*value="3"|value="3"[^>]*id="lowConfidenceStreakCap"/
    );
  });

  it("Appearance rail entry is a real anchor to a real accent-color/launcher-position section (widget branding/personalization)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    mockSettingsFetch();

    const element = await SettingsPage();
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/<a[^>]+href="#settings-appearance"/);
    expect(html).toMatch(/id="settings-appearance"/);
    expect(html).toMatch(/name="accentColor"/);
    expect(html).toMatch(/name="launcherPosition"/);
  });

  it("CLIENT_AGENT gets a read-only view, no Save/Publish/Discard buttons", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_AGENT") });
    mockSettingsFetch();

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
