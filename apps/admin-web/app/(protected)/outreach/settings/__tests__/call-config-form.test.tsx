/**
 * Live 2026-11-28 (HomeOwners): a homeowner was asked for their company and told "our
 * marketing expert" would answer. Each chatbot sets who answers and whether to ask.
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/lib/outreach", () => ({
  addSuppression: vi.fn(),
  removeSuppression: vi.fn(),
  saveAgentSettings: vi.fn(),
  saveCallAgentConfig: vi.fn(),
  saveEmailConfig: vi.fn(),
  saveQualification: vi.fn(),
}));

import { CallConfigForm } from "../settings-forms";

describe("AI calling form", () => {
  it("shows the chatbot's own expert and its company-question choice", () => {
    const html = renderToStaticMarkup(
      <CallConfigForm
        config={{ configured: true, phoneProvider: "telnyx", expertTitle: "roofing specialist", askCompany: false }}
      />
    );
    expect(html).toContain('name="expert_title"');
    expect(html).toContain('value="roofing specialist"');
    expect(html).toContain('name="ask_company"/>'); // unticked
  });

  it("keeps today's wording for a chatbot that never set them", () => {
    const html = renderToStaticMarkup(<CallConfigForm config={{ configured: true, phoneProvider: "telnyx" }} />);
    expect(html).toContain('value="marketing expert"');
    expect(html).toMatch(/name="ask_company"[^>]*checked/);
  });
});
