/**
 * Onboarding checklist -- tests for `isOnboardingComplete` and
 * <OnboardingChecklist>, using this repo's established `environment: "node"`
 * `renderToStaticMarkup` pattern (see coverage-gaps.test.tsx's header
 * comment for the full rationale).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { OnboardingChecklist, isOnboardingComplete } from "@/app/(protected)/onboarding-checklist";
import type { SettingsResult, BotSettings } from "@/lib/settings";
import type { ListKnowledgeResult } from "@/app/(protected)/knowledge/actions";
import type { CallConfigResult } from "@/lib/calls";

const BLANK_SETTINGS: BotSettings = {
  greeting: null,
  launcherLabel: null,
  sidebarWorkspaceLabel: null,
  dashboardTitle: null,
  botName: null,
  accentColor: null,
  launcherPosition: null,
  suggestedQuestions: null,
  businessHours: null,
  escalationPolicy: null,
  tone: null,
  answerThreshold: 0.5,
  escalateThreshold: 0.3,
  turnCap: 6,
  lowConfidenceStreakCap: 3,
  llmProvider: null,
  llmModel: null,
};

function settingsOk(overrides: Partial<BotSettings> = {}): SettingsResult {
  return { status: "ok", settings: { ...BLANK_SETTINGS, ...overrides } };
}

function settingsError(): SettingsResult {
  return { status: "error", message: "Something went wrong.", correlationId: "corr-1" };
}

function docsOk(count: number): ListKnowledgeResult {
  return {
    status: "ok",
    docs: Array.from({ length: count }, (_, i) => ({
      docId: `doc-${i}`,
      title: null,
      description: null,
      filename: `doc-${i}.txt`,
      contentType: "text/plain",
      status: "ready",
      uploadedBy: null,
      uploadedByName: null,
      createdAt: "2026-01-01T00:00:00Z",
    })),
  };
}

function docsError(): ListKnowledgeResult {
  return { status: "error", message: "Something went wrong.", correlationId: "corr-2" };
}

function callConfigOk(overrides: Partial<{ monitoredPhoneNumber: string | null; enabled: boolean }> = {}): CallConfigResult {
  return {
    status: "ok",
    config: {
      monitoredPhoneNumber: null,
      enabled: false,
      textBackMessage: null,
      ...overrides,
    },
  };
}

describe("isOnboardingComplete", () => {
  it("is false when settings are still blank and no docs exist", () => {
    expect(isOnboardingComplete(settingsOk(), docsOk(0))).toBe(false);
  });

  it("is false when settings are customized but no docs exist", () => {
    expect(isOnboardingComplete(settingsOk({ greeting: "Hi!" }), docsOk(0))).toBe(false);
  });

  it("is false when docs exist but settings are still blank", () => {
    expect(isOnboardingComplete(settingsOk(), docsOk(1))).toBe(false);
  });

  it("is true once settings are customized AND at least one doc exists", () => {
    expect(isOnboardingComplete(settingsOk({ tone: "friendly" }), docsOk(1))).toBe(true);
  });

  it("treats a settings fetch error as incomplete, never fabricating done", () => {
    expect(isOnboardingComplete(settingsError(), docsOk(1))).toBe(false);
  });

  it("treats a docs fetch error as incomplete, never fabricating done", () => {
    expect(isOnboardingComplete(settingsOk({ greeting: "Hi!" }), docsError())).toBe(false);
  });

  it("businessHours alone (non-empty object) counts as customized", () => {
    expect(
      isOnboardingComplete(settingsOk({ businessHours: { mon: ["09:00", "17:00"] } }), docsOk(1))
    ).toBe(true);
  });
});

describe("OnboardingChecklist", () => {
  it("renders both required items incomplete with working links when nothing is set up", () => {
    const html = renderToStaticMarkup(
      <OnboardingChecklist
        settingsResult={settingsOk()}
        docsResult={docsOk(0)}
        callConfigResult={callConfigOk()}
        settingsHref="/settings"
        knowledgeHref="/knowledge"
      />
    );

    expect(html).toContain("Customize your bot");
    expect(html).toContain("Upload your knowledge base");
    expect(html).toContain('href="/settings"');
    expect(html).toContain('href="/knowledge"');
    expect(html).not.toContain("line-through");
  });

  it("renders the Test your bot item as a plain action link with no checkbox state", () => {
    const html = renderToStaticMarkup(
      <OnboardingChecklist
        settingsResult={settingsOk()}
        docsResult={docsOk(0)}
        callConfigResult={callConfigOk()}
        settingsHref="/settings"
        knowledgeHref="/knowledge"
      />
    );

    expect(html).toContain("Test your bot");
    expect(html).toContain("Try it now");
  });

  it("marks settings and knowledge done (struck through, no CTA link) once both are complete", () => {
    const html = renderToStaticMarkup(
      <OnboardingChecklist
        settingsResult={settingsOk({ greeting: "Hi there!" })}
        docsResult={docsOk(2)}
        callConfigResult={callConfigOk()}
        settingsHref="/settings"
        knowledgeHref="/knowledge"
      />
    );

    expect(html).toContain("line-through");
    // The "Go to settings"/"Upload documents" CTA links only render when NOT done.
    expect(html).not.toContain("Go to settings");
    expect(html).not.toContain("Upload documents");
  });

  it("shows the 'Get your bot ready' heading while the two required items are still incomplete", () => {
    const html = renderToStaticMarkup(
      <OnboardingChecklist
        settingsResult={settingsOk()}
        docsResult={docsOk(0)}
        callConfigResult={callConfigOk()}
        settingsHref="/settings"
        knowledgeHref="/knowledge"
      />
    );

    expect(html).toContain("Get your bot ready");
    expect(html).not.toContain("Your bot is set up");
  });

  it("switches to the 'Your bot is set up' heading once both required items are done, even with missed-call still unset", () => {
    const html = renderToStaticMarkup(
      <OnboardingChecklist
        settingsResult={settingsOk({ greeting: "Hi there!" })}
        docsResult={docsOk(1)}
        callConfigResult={callConfigOk()}
        settingsHref="/settings"
        knowledgeHref="/knowledge"
      />
    );

    expect(html).toContain("Your bot is set up");
    expect(html).not.toContain("Get your bot ready");
  });

  it("labels missed-call text-back as optional and never treats it as done from a fetch error", () => {
    const html = renderToStaticMarkup(
      <OnboardingChecklist
        settingsResult={settingsOk({ greeting: "Hi!" })}
        docsResult={docsOk(1)}
        callConfigResult={{ status: "error", message: "x", correlationId: "c" }}
        settingsHref="/settings"
        knowledgeHref="/knowledge"
      />
    );

    expect(html).toContain("Set up missed-call text-back (optional)");
    expect(html).toContain("Set up");
  });

  it("marks missed-call text-back done only when a monitored number is set AND enabled", () => {
    const html = renderToStaticMarkup(
      <OnboardingChecklist
        settingsResult={settingsOk({ greeting: "Hi!" })}
        docsResult={docsOk(1)}
        callConfigResult={callConfigOk({ monitoredPhoneNumber: "+15005550006", enabled: true })}
        settingsHref="/settings"
        knowledgeHref="/knowledge"
      />
    );

    // Only the (still-incomplete) "Test your bot" CTA remains -- missed-call's own "Set up" link is gone.
    const setupLinks = (html.match(/Set up</g) ?? []).length;
    expect(setupLinks).toBe(0);
  });
});
