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
  phone: null,
  status: "open",
  stage: "qualified",
  qualificationScore: 80,
  assignedAgentId: null,
  source: "widget",
};

describe("LeadDrawer -- SR-17 D3 Timeline tab (shared with ContactDrawer)", () => {
  it("SR-24: has exactly three tabs (Timeline/Details/Notes) -- Transcript and Activity are deleted", () => {
    const html = renderToStaticMarkup(
      <LeadDrawer
        leadId="lead-1"
        tab="timeline"
        detailResult={{ status: "ok", lead: baseLead }}
        activitiesResult={null}
        timelineResult={null}
        basePath="/leads"
      />
    );

    expect(html).toMatch(/Timeline/);
    expect(html).toMatch(/Details/);
    expect(html).toMatch(/Notes/);
    expect(html).not.toMatch(/Transcript/);
    expect(html).not.toMatch(/>Activity</);
  });

  it("SR-24: defaults to the Timeline tab when no tab prop override is applied by the container", () => {
    const html = renderToStaticMarkup(
      <LeadDrawer
        leadId="lead-1"
        tab="timeline"
        detailResult={{ status: "ok", lead: baseLead }}
        activitiesResult={null}
        timelineResult={null}
        basePath="/leads"
      />
    );

    expect(html).toMatch(/id="lead-tab-timeline"[^>]*aria-selected="true"/);
  });

  it("renders the SAME degradation notice on the Timeline tab as the Contact drawer (D3/D4)", () => {
    const html = renderToStaticMarkup(
      <LeadDrawer
        leadId="lead-1"
        tab="timeline"
        detailResult={{ status: "ok", lead: baseLead }}
        activitiesResult={null}
        timelineResult={{
          status: "ok",
          data: {
            subject: { kind: "lead", id: "lead-1", convertedToContactId: null },
            degraded: true,
            sources: { bookings: { state: "failed", count: 0, truncated: false } },
            items: [],
            nextBefore: null,
          },
        }}
        basePath="/leads"
      />
    );

    expect(html).toMatch(/incomplete/i);
    expect(html).toMatch(/bookings/);
  });

  it("degraded:false on the lead timeline renders no notice", () => {
    const html = renderToStaticMarkup(
      <LeadDrawer
        leadId="lead-1"
        tab="timeline"
        detailResult={{ status: "ok", lead: baseLead }}
        activitiesResult={null}
        timelineResult={{
          status: "ok",
          data: {
            subject: { kind: "lead", id: "lead-1", convertedToContactId: null },
            degraded: false,
            sources: {},
            items: [],
            nextBefore: null,
          },
        }}
        basePath="/leads"
      />
    );

    expect(html).not.toMatch(/incomplete/i);
  });

  it("an empty lead timeline (not degraded) renders 'No activity yet'", () => {
    const html = renderToStaticMarkup(
      <LeadDrawer
        leadId="lead-1"
        tab="timeline"
        detailResult={{ status: "ok", lead: baseLead }}
        activitiesResult={null}
        timelineResult={{
          status: "ok",
          data: {
            subject: { kind: "lead", id: "lead-1", convertedToContactId: null },
            degraded: false,
            sources: {},
            items: [],
            nextBefore: null,
          },
        }}
        basePath="/leads"
      />
    );

    expect(html).toMatch(/No activity yet/i);
  });

  it("a null timelineResult (tab not active yet) renders a loading state, not an error", () => {
    const html = renderToStaticMarkup(
      <LeadDrawer
        leadId="lead-1"
        tab="timeline"
        detailResult={{ status: "ok", lead: baseLead }}
        activitiesResult={null}
        timelineResult={null}
        basePath="/leads"
      />
    );

    expect(html).toMatch(/Loading timeline/i);
  });

  it("does NOT change the existing Board/Table toggle or the Leads/Contacts/Companies tab strip (out of scope: leads table itself)", () => {
    const html = renderToStaticMarkup(
      <LeadDrawer
        leadId="lead-1"
        tab="timeline"
        detailResult={{ status: "ok", lead: baseLead }}
        activitiesResult={null}
        timelineResult={null}
        basePath="/leads"
      />
    );

    expect(html).not.toMatch(/Companies/);
  });
});
