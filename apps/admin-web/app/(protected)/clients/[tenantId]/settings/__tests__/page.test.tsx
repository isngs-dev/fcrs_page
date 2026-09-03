/**
 * Regression test for the "compressed fields" layout bug: this screen used
 * to wrap `<SettingsForm>` in a `max-w-2xl` `<Card>`, squeezing its
 * three-column shell (184px rail + content + 300px preview, `lg:flex-row`)
 * into a viewport far narrower than it needs -- every input collapsed to a
 * sliver. `renderToStaticMarkup` can't measure actual pixel widths (no
 * layout engine), so the assertion below encodes the fix structurally: there
 * used to be TWO `max-w-2xl` containers on this page (the OnboardingChecklist
 * wrapper, and the removed Card around SettingsForm) -- now there is exactly
 * one, and `<SettingsForm>`'s own rail renders unconstrained.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const adminApiFetchMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (path: string, init?: RequestInit) => adminApiFetchMock(path, init),
  };
});

function jsonResponse(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

const SETTINGS_BODY = {
  greeting: "Hi there!",
  launcher_label: null,
  sidebar_workspace_label: null,
  dashboard_title: null,
  bot_name: null,
  accent_color: null,
  launcher_position: null,
  suggested_questions: null,
  business_hours: null,
  escalation_policy: null,
  tone: null,
  answer_threshold: 0.5,
  escalate_threshold: 0.35,
  turn_cap: 6,
  low_confidence_streak_cap: 3,
  llm_provider: null,
  llm_model: null,
};

const CALL_CONFIG_BODY = {
  monitored_phone_number: null,
  enabled: false,
  text_back_message: null,
};

const KNOWLEDGE_DOCS_BODY = { docs: [] };

const ClientSettingsPage = (await import("@/app/(protected)/clients/[tenantId]/settings/page"))
  .default;

describe("ClientSettingsPage (/clients/[tenantId]/settings)", () => {
  afterEach(() => {
    adminApiFetchMock.mockReset();
  });

  it("renders SettingsForm unwrapped -- exactly one max-w-2xl container on the page (the checklist), not two", async () => {
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path.includes("/settings")) return Promise.resolve(jsonResponse(SETTINGS_BODY));
      if (path.includes("/calls/config")) return Promise.resolve(jsonResponse(CALL_CONFIG_BODY));
      if (path.includes("/ingestion/docs")) return Promise.resolve(jsonResponse(KNOWLEDGE_DOCS_BODY));
      return Promise.resolve(jsonResponse({}, 404));
    });

    const element = await ClientSettingsPage({ params: Promise.resolve({ tenantId: "tenant-1" }) });
    const html = renderToStaticMarkup(element);

    const maxW2xlCount = (html.match(/max-w-2xl/g) ?? []).length;
    expect(maxW2xlCount).toBe(1);

    // SettingsForm's own 184px section rail rendered -- proof it mounted at
    // all, not squeezed inside a since-removed narrow ancestor.
    expect(html).toContain("w-[184px]");
    expect(html).toContain("Bot settings");
    // The removed Card's duplicate title text is gone.
    expect(html).not.toContain("This client&#x27;s chatbot configuration.");
  });

  it("still shows an honest error state (not SettingsForm) when the settings fetch fails", async () => {
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path.includes("/settings")) return Promise.reject(new Error("boom"));
      if (path.includes("/calls/config")) return Promise.resolve(jsonResponse(CALL_CONFIG_BODY));
      if (path.includes("/ingestion/docs")) return Promise.resolve(jsonResponse(KNOWLEDGE_DOCS_BODY));
      return Promise.resolve(jsonResponse({}, 404));
    });

    const element = await ClientSettingsPage({ params: Promise.resolve({ tenantId: "tenant-1" }) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toContain("w-[184px]");
    expect(html).toMatch(/role="alert"/);
  });
});
