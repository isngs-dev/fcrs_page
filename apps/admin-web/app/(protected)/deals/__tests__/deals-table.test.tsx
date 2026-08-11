import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { Deal } from "@/lib/deals";
import { DealsTable } from "@/app/(protected)/deals/deals-table";

function makeDeal(overrides: Partial<Deal> = {}): Deal {
  return {
    opportunityId: "opp-1",
    contactId: "contact-1",
    accountId: "account-1",
    name: "Acme renewal",
    amount: "1500.00",
    currency: "USD",
    stage: "prospecting",
    winProbability: 10,
    expectedCloseDate: "2026-09-01",
    closedAt: null,
    closeReason: null,
    ownerAgentId: "agent-1",
    createdAt: "2026-07-15T00:00:00Z",
    ...overrides,
  };
}

describe("DealsTable -- D6 money honesty (MANDATORY)", () => {
  it("renders amount:null as 'Not quoted', NEVER '$0' or '0'", () => {
    const html = renderToStaticMarkup(<DealsTable items={[makeDeal({ amount: null })]} />);
    expect(html).toMatch(/Not quoted/);
    expect(html).not.toMatch(/\$0\b/);
    expect(html).not.toMatch(/>0</);
  });

  it("renders a real amount with its row's own currency code, never a bare $ sign", () => {
    const html = renderToStaticMarkup(<DealsTable items={[makeDeal({ amount: "2500.00", currency: "EUR" })]} />);
    expect(html).toMatch(/2500\.00\s*EUR/);
    expect(html).not.toMatch(/\$/);
  });

  it("two rows with different currencies each render their OWN currency code", () => {
    const html = renderToStaticMarkup(
      <DealsTable
        items={[
          makeDeal({ opportunityId: "opp-eur", amount: "100.00", currency: "EUR" }),
          makeDeal({ opportunityId: "opp-usd", amount: "200.00", currency: "USD" }),
        ]}
      />
    );
    expect(html).toMatch(/100\.00\s*EUR/);
    expect(html).toMatch(/200\.00\s*USD/);
  });

  it("a $0.00 amount (a real free/pro-bono deal) renders distinctly from 'Not quoted'", () => {
    const html = renderToStaticMarkup(<DealsTable items={[makeDeal({ amount: "0.00", currency: "USD" })]} />);
    expect(html).toMatch(/0\.00\s*USD/);
    expect(html).not.toMatch(/Not quoted/);
  });

  it("renders NO column total / pipeline sum anywhere (D6: no client-side money arithmetic)", () => {
    const html = renderToStaticMarkup(
      <DealsTable
        items={[
          makeDeal({ opportunityId: "opp-1", amount: "100.00" }),
          makeDeal({ opportunityId: "opp-2", amount: "200.00" }),
        ]}
      />
    );
    // "300" would only appear if something summed the two amounts.
    expect(html).not.toMatch(/300/);
    expect(html).not.toMatch(/[Tt]otal/);
  });

  it("renders a link, not a raw account id, when accountId is present (mirrors ContactsTable D5)", () => {
    const html = renderToStaticMarkup(<DealsTable items={[makeDeal({ accountId: "account-1" })]} />);
    expect(html).toMatch(/View account/);
    expect(html).not.toMatch(/>account-1</);
  });

  it("renders a muted dash when accountId is null", () => {
    const html = renderToStaticMarkup(<DealsTable items={[makeDeal({ accountId: null })]} />);
    expect(html).not.toMatch(/View account\b/);
  });

  it("win probability is rendered nowhere as an <input>/<select> (display-only, D6)", () => {
    const html = renderToStaticMarkup(<DealsTable items={[makeDeal({ winProbability: 42 })]} />);
    expect(html).not.toMatch(/<input/);
    expect(html).not.toMatch(/<select/);
  });
});
