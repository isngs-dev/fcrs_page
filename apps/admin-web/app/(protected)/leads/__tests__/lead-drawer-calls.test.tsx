import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const { LeadDrawer } = await import("@/app/(protected)/leads/lead-drawer");

const baseLead = {
  leadId: "lead-1",
  name: "Ada Lovelace",
  email: "ada@example.com",
  phone: "(555) 123-4567",
  status: "open",
  stage: "qualified",
  qualificationScore: 80,
  assignedAgentId: null,
  source: "widget",
};

function render(voiceCallsResult: Parameters<typeof LeadDrawer>[0]["voiceCallsResult"]): string {
  return renderToStaticMarkup(
    <LeadDrawer
      leadId="lead-1"
      tab="calls"
      detailResult={{ status: "ok", lead: baseLead }}
      activitiesResult={null}
      timelineResult={null}
      voiceCallsResult={voiceCallsResult}
      basePath="/leads"
    />
  );
}

describe("LeadDrawer -- AI call tab", () => {
  it("shows each question with the visitor's yes/no answer and what was heard", () => {
    const html = render({
      status: "ok",
      calls: [
        {
          callId: "call-1",
          status: "completed",
          toNumber: "+15551234567",
          createdAt: "2026-10-01T15:00:00Z",
          lastError: null,
          transcript: [
            { question: "Can you make it on Monday?", heard: "yes I can", answer: "yes" },
            { question: "Is it leaking?", heard: "pressed 2", answer: "no" },
          ],
        },
      ],
    });

    expect(html).toMatch(/id="lead-tab-calls"[^>]*aria-selected="true"/);
    expect(html).toContain("Completed");
    expect(html).toContain("Can you make it on Monday?");
    expect(html).toContain("Is it leaking?");
    expect(html).toMatch(/>Yes</);
    expect(html).toMatch(/>No</);
    expect(html).toContain("heard: &quot;yes I can&quot;");
  });

  it("shows why a call failed (e.g. Twilio not set up yet)", () => {
    const html = render({
      status: "ok",
      calls: [
        {
          callId: "call-2",
          status: "failed",
          toNumber: "+15551234567",
          createdAt: "2026-10-01T15:00:00Z",
          lastError: "TWILIO_NOT_CONFIGURED",
          transcript: [],
        },
      ],
    });

    expect(html).toContain("Failed");
    expect(html).toContain("TWILIO_NOT_CONFIGURED");
  });

  it("explains when there is no call yet", () => {
    expect(render({ status: "ok", calls: [] })).toContain("No AI confirmation call for this lead");
  });
});
