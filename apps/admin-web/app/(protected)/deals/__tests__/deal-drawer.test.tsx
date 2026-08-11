import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const { DealDrawer } = await import("@/app/(protected)/deals/deal-drawer");

const baseDeal = {
  opportunityId: "opp-1",
  contactId: "contact-1",
  accountId: "account-1",
  name: "Acme renewal",
  amount: null,
  currency: "USD",
  stage: "prospecting",
  winProbability: 10,
  expectedCloseDate: "2026-09-01",
  closedAt: null,
  closeReason: null,
  ownerAgentId: "agent-1",
  createdAt: "2026-07-15T00:00:00Z",
};

describe("DealDrawer -- scope item 7: no opportunity timeline item kind exists yet, must not fabricate one", () => {
  it("does NOT render a fabricated activity/event feed for the deal", () => {
    const html = renderToStaticMarkup(
      <DealDrawer opportunityId="opp-1" detailResult={{ status: "ok", deal: baseDeal }} basePath="/deals" />
    );

    // No stage_change/note/assignment-style activity rows -- those are lead
    // activity types (leads/admin_routes.py), never fabricated for a deal.
    expect(html).not.toMatch(/stage_change/);
    expect(html).not.toMatch(/converted_to_contact/);
  });

  it("renders an honest notice that deal activity history is not tracked yet", () => {
    const html = renderToStaticMarkup(
      <DealDrawer opportunityId="opp-1" detailResult={{ status: "ok", deal: baseDeal }} basePath="/deals" />
    );

    expect(html).toMatch(/not tracked yet/i);
  });

  it("renders the deal's amount as 'Not quoted' when null, never $0 (D6)", () => {
    const html = renderToStaticMarkup(
      <DealDrawer opportunityId="opp-1" detailResult={{ status: "ok", deal: baseDeal }} basePath="/deals" />
    );

    expect(html).toMatch(/Not quoted/);
  });

  it("renders win probability as derived text, never an <input>/<select>", () => {
    const html = renderToStaticMarkup(
      <DealDrawer opportunityId="opp-1" detailResult={{ status: "ok", deal: baseDeal }} basePath="/deals" />
    );

    expect(html).toMatch(/derived, not editable/i);
    expect(html).not.toMatch(/<input/);
    expect(html).not.toMatch(/<select/);
  });

  it("renders the error state honestly when the detail fetch failed", () => {
    const html = renderToStaticMarkup(
      <DealDrawer
        opportunityId="opp-1"
        detailResult={{ status: "error", message: "This deal could not be found.", correlationId: "corr-1" }}
        basePath="/deals"
      />
    );

    expect(html).toMatch(/could not be found/);
    expect(html).toMatch(/corr-1/);
  });
});
