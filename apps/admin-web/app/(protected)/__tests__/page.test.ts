/**
 * SR-16: tests for the rebuilt dashboard landing page
 * (`app/(protected)/page.tsx`).
 *
 * This codebase has no React Testing Library / jsdom (vitest.config.ts runs
 * `environment: "node"` and only includes `**\/*.test.ts`, never `.tsx`) --
 * every existing page/server-component test in this repo (see
 * `lib/__tests__/require-role.test.ts`) exercises the async server function
 * directly: mock `next/headers`/`next/navigation` + the data layer, call the
 * exported page function, and assert on the thrown redirect or the resolved
 * JSX tree's top-level shape via `React.isValidElement`/prop inspection.
 * This suite follows that same technique rather than inventing a
 * render-to-DOM path this project doesn't otherwise use (no new dependency,
 * per CLAUDE.md §4 / the sprint's own constraints).
 *
 * Coverage: RBAC-aware rendering (MANDATORY), no-silent-fallback error
 * state (MANDATORY), and deletion-completeness (D1) both by control-flow
 * (getDashboardPipeline is never imported/called) and by static source
 * inspection (no LeadCard/PipelineColumn/StageDistributionChart/"Lead
 * pipeline" text survives in the page module or its new dashboard
 * children).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const redirectMock = vi.fn((url: string) => {
  throw new Error(`REDIRECT:${url}`);
});
const getChatbotHubMock = vi.fn();
const getBotSettingsMock = vi.fn();

vi.mock("next/navigation", () => ({
  redirect: redirectMock,
}));

vi.mock("@/lib/hub", async () => {
  const actual = await vi.importActual<typeof import("@/lib/hub")>("@/lib/hub");
  return { ...actual, getChatbotHub: getChatbotHubMock };
});

vi.mock("@/lib/settings", () => ({
  getBotSettings: getBotSettingsMock,
}));

const { default: ProtectedHomePage } = await import("@/app/(protected)/page");

/** Strips `/* ... *\/` block comments and `// ...` line comments so the
 * deletion-completeness checks below assert on actual CODE (imports, JSX,
 * identifiers) rather than tripping on this sprint's own explanatory prose
 * about what was deleted (e.g. this page's header comment names
 * `getDashboardPipeline`/`LeadCard` deliberately, to document the deletion --
 * that prose must not itself fail the "is it gone" check). */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}

function makeSearchParams(params: Record<string, string> = {}) {
  return Promise.resolve(params);
}

const OK_HUB = {
  status: "ok" as const,
  data: {
    analytics: {
      window: { from: "2026-07-01T00:00:00.000Z", to: "2026-07-08T00:00:00.000Z", bucket: "day" },
      totals: { conversations: 82, userTurns: 200, botTurns: 190, decidedBotTurns: 180 },
      intentDistribution: {},
      decisionDistribution: {},
      fallbackRate: 0.11,
      deflectionRate: 0.72,
      groundedRate: 0.93,
      schedule: { ctaConversations: 10, conversions: 3, conversionRate: 0.3 },
      series: [
        { bucketStart: "2026-07-01T00:00:00.000Z", conversations: 12, answers: 9, escalations: 3, bookings: 1 },
      ],
    },
    activeConversations: {
      total: 5,
      items: [
        {
          conversationId: "conv-9f2a",
          status: "active",
          channel: "widget",
          startedAt: "2026-07-20T10:00:00.000Z",
          messageCount: 3,
          summary: "Asked about pricing",
        },
      ],
    },
    closedConversations: { total: 20 },
    period: "month" as const,
    bucket: "day" as const,
  },
};

const ZERO_HUB = {
  status: "ok" as const,
  data: {
    analytics: {
      window: { from: "2026-07-01T00:00:00.000Z", to: "2026-07-08T00:00:00.000Z", bucket: "day" },
      totals: { conversations: 0, userTurns: 0, botTurns: 0, decidedBotTurns: 0 },
      intentDistribution: {},
      decisionDistribution: {},
      fallbackRate: null,
      deflectionRate: null,
      groundedRate: null,
      schedule: { ctaConversations: 0, conversions: 0, conversionRate: null },
      series: [],
    },
    activeConversations: { total: 0, items: [] },
    closedConversations: { total: 0 },
    period: "month" as const,
    bucket: "day" as const,
  },
};

const ERROR_HUB = {
  status: "error" as const,
  message: "Chatbot hub data could not be loaded. Please try again.",
  correlationId: "corr-abc-123",
};

// `require-role.test.ts` signs real JWTs through the real `verifyToken` to
// exercise `next/headers` end to end. This page only calls `getClaims()`
// (not `requireRole`), so it's simpler and equally honest to mock
// `@/lib/auth` directly rather than re-deriving JWT signing here.
vi.mock("@/lib/auth", () => ({
  getClaims: vi.fn(),
}));

const { getClaims } = await import("@/lib/auth");
const getClaimsMock = vi.mocked(getClaims);

describe("ProtectedHomePage RBAC (MANDATORY -- CLAUDE.md §3)", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("redirects unauthenticated visitors to /login -- never serves a shell with empty widgets", async () => {
    getClaimsMock.mockResolvedValue(null);
    await expect(ProtectedHomePage({ searchParams: makeSearchParams() })).rejects.toThrow("REDIRECT:/login");
    expect(getChatbotHubMock).not.toHaveBeenCalled();
  });

  it("PLATFORM_ADMIN's landing behaviour is unchanged by this rewrite -- redirects to /clients", async () => {
    getClaimsMock.mockResolvedValue({ subject: "p1", role: "PLATFORM_ADMIN", tenantId: null, projectIds: [] });
    await expect(ProtectedHomePage({ searchParams: makeSearchParams() })).rejects.toThrow("REDIRECT:/clients");
    expect(getChatbotHubMock).not.toHaveBeenCalled();
  });

  it("CLIENT_ADMIN renders the dashboard", async () => {
    getClaimsMock.mockResolvedValue({ subject: "a1", role: "CLIENT_ADMIN", tenantId: "t1", projectIds: [] });
    getBotSettingsMock.mockResolvedValue({ status: "ok", settings: { dashboardTitle: "Dashboard" } });
    getChatbotHubMock.mockResolvedValue(OK_HUB);

    const element = (await ProtectedHomePage({ searchParams: makeSearchParams() })) as ReactElement;
    expect(element).toBeTruthy();
    expect(getChatbotHubMock).toHaveBeenCalledTimes(1);
  });

  it("CLIENT_AGENT renders the dashboard identically (both roles are in the nav, SR-15 M6)", async () => {
    getClaimsMock.mockResolvedValue({ subject: "ag1", role: "CLIENT_AGENT", tenantId: "t1", projectIds: [] });
    getBotSettingsMock.mockResolvedValue({ status: "ok", settings: { dashboardTitle: "Dashboard" } });
    getChatbotHubMock.mockResolvedValue(OK_HUB);

    const element = (await ProtectedHomePage({ searchParams: makeSearchParams() })) as ReactElement;
    expect(element).toBeTruthy();
    expect(getChatbotHubMock).toHaveBeenCalledTimes(1);
  });

  it("VISITOR role (should never reach this route) is redirected to /login, not served the dashboard", async () => {
    getClaimsMock.mockResolvedValue({ subject: "v1", role: "VISITOR", tenantId: "t1", projectIds: [] });
    await expect(ProtectedHomePage({ searchParams: makeSearchParams() })).rejects.toThrow("REDIRECT:/login");
  });
});

describe("ProtectedHomePage multi-tenant safety (MANDATORY -- CLAUDE.md §3)", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("never passes a tenantId from search params through to getChatbotHub -- only period/bucket", async () => {
    getClaimsMock.mockResolvedValue({ subject: "a1", role: "CLIENT_ADMIN", tenantId: "t1", projectIds: [] });
    getBotSettingsMock.mockResolvedValue({ status: "ok", settings: { dashboardTitle: "Dashboard" } });
    getChatbotHubMock.mockResolvedValue(OK_HUB);

    await ProtectedHomePage({
      searchParams: makeSearchParams({ tenantId: "attacker-tenant", period: "week", bucket: "day" }),
    });

    expect(getChatbotHubMock).toHaveBeenCalledWith({ period: "week", bucket: "day" });
    const [[calledWith]] = getChatbotHubMock.mock.calls;
    expect(calledWith).not.toHaveProperty("tenantId");
  });

  it("issues no new fetch beyond the existing server-only lib/hub.ts + lib/settings.ts reads", async () => {
    getClaimsMock.mockResolvedValue({ subject: "a1", role: "CLIENT_ADMIN", tenantId: "t1", projectIds: [] });
    getBotSettingsMock.mockResolvedValue({ status: "ok", settings: { dashboardTitle: "Dashboard" } });
    getChatbotHubMock.mockResolvedValue(OK_HUB);

    await ProtectedHomePage({ searchParams: makeSearchParams() });

    expect(getChatbotHubMock).toHaveBeenCalledTimes(1);
    expect(getBotSettingsMock).toHaveBeenCalledTimes(1);
  });
});

describe("ProtectedHomePage no-silent-fallback (MANDATORY -- the highest-value tests in this sprint)", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("a backend error renders the error treatment with its correlation ID, not an empty/zeroed dashboard", async () => {
    getClaimsMock.mockResolvedValue({ subject: "a1", role: "CLIENT_ADMIN", tenantId: "t1", projectIds: [] });
    getBotSettingsMock.mockResolvedValue({ status: "ok", settings: { dashboardTitle: "Dashboard" } });
    getChatbotHubMock.mockResolvedValue(ERROR_HUB);

    const element = (await ProtectedHomePage({ searchParams: makeSearchParams() })) as ReactElement;
    const serialized = JSON.stringify(element, (_key, value) =>
      typeof value === "function" ? undefined : value
    );
    expect(serialized).toContain("could not be loaded");
    expect(serialized).toContain(ERROR_HUB.correlationId);
  });

  it("a zero-conversation tenant still resolves to status 'ok' and renders the dashboard tree (honest empty states live inside the widgets, not as a page-level failure)", async () => {
    getClaimsMock.mockResolvedValue({ subject: "a1", role: "CLIENT_ADMIN", tenantId: "t1", projectIds: [] });
    getBotSettingsMock.mockResolvedValue({ status: "ok", settings: { dashboardTitle: "Dashboard" } });
    getChatbotHubMock.mockResolvedValue(ZERO_HUB);

    const element = (await ProtectedHomePage({ searchParams: makeSearchParams() })) as ReactElement;
    expect(element).toBeTruthy();
    const serialized = JSON.stringify(element, (_key, value) =>
      typeof value === "function" ? undefined : value
    );
    // Must not contain the error banner's text when the hub itself is "ok".
    expect(serialized).not.toContain("could not be loaded");
  });
});

describe("ProtectedHomePage deletion-completeness (D1)", () => {
  it("getDashboardPipeline is never imported or called from the dashboard route", async () => {
    // Structural check on the actual source text -- guards against the
    // kanban silently returning via a re-added import, not just against
    // this test file's own mocks (which wouldn't fail if the import were
    // simply unused-but-present).
    const pagePath = fileURLToPath(new URL("../page.tsx", import.meta.url));
    const source = stripComments(readFileSync(pagePath, "utf-8"));
    expect(source).not.toMatch(/getDashboardPipeline/);
    expect(source).not.toMatch(/lib\/dashboard/);
  });

  it("renders no lead card, stage column, or 'Lead pipeline' section -- asserted by absence in source, so it cannot return by a later silent merge", async () => {
    const pagePath = fileURLToPath(new URL("../page.tsx", import.meta.url));
    const source = stripComments(readFileSync(pagePath, "utf-8"));
    for (const forbidden of ["LeadCard", "PipelineColumn", "StageDistributionChart", "QualificationDonut", "Lead pipeline"]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("the new dashboard child modules also carry no lead-pipeline references", async () => {
    const files = ["../dashboard-hero.tsx", "../dashboard-responses-card.tsx", "../dashboard-metric-cards.tsx"];
    for (const relative of files) {
      const filePath = fileURLToPath(new URL(relative, import.meta.url));
      const source = stripComments(readFileSync(filePath, "utf-8"));
      for (const forbidden of ["LeadCard", "PipelineColumn", "StageDistributionChart", "getDashboardPipeline"]) {
        expect(source).not.toContain(forbidden);
      }
    }
  });
});

describe("ProtectedHomePage peak-hours heatmap (D7 -- not built, stubbed, or referenced)", () => {
  it("no dashboard module references a peak-hours heatmap", async () => {
    const files = ["../page.tsx", "../dashboard-hero.tsx", "../dashboard-responses-card.tsx", "../dashboard-metric-cards.tsx"];
    for (const relative of files) {
      const filePath = fileURLToPath(new URL(relative, import.meta.url));
      const source = readFileSync(filePath, "utf-8").toLowerCase();
      expect(source).not.toContain("heatmap");
      expect(source).not.toContain("peak hour");
      expect(source).not.toContain("peak-hour");
    }
  });
});

describe("ProtectedHomePage bucket selector (D4 -- day/week only, no month)", () => {
  it("the responses card source offers no 'month' option", async () => {
    const filePath = fileURLToPath(new URL("../dashboard-responses-card.tsx", import.meta.url));
    const source = readFileSync(filePath, "utf-8");
    expect(source).not.toMatch(/"month"/);
    expect(source).toContain("HUB_BUCKETS");
  });
});
