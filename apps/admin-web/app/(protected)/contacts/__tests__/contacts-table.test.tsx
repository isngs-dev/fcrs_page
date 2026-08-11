import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { Contact } from "@/lib/contacts";
import { ContactsTable } from "@/app/(protected)/contacts/contacts-table";

function makeContact(overrides: Partial<Contact> = {}): Contact {
  return {
    contactId: "contact-1",
    accountId: null,
    leadId: null,
    name: "Ada Lovelace",
    email: "ada@example.com",
    phone: null,
    ownerAgentId: null,
    createdAt: "2026-07-15T00:00:00Z",
    ...overrides,
  };
}

describe("ContactsTable (D5, name resolution SR-27 slice 5)", () => {
  it("falls back to the honest 'View in Accounts' link when accountId isn't in the map", () => {
    const html = renderToStaticMarkup(<ContactsTable items={[makeContact({ accountId: "account-1" })]} />);
    expect(html).toMatch(/View in Accounts/);
    // Never a raw id string rendered as the cell's visible text.
    expect(html).not.toMatch(/>account-1</);
  });

  it("renders the REAL resolved account name when accountNames map has it (G8)", () => {
    const html = renderToStaticMarkup(
      <ContactsTable
        items={[makeContact({ accountId: "account-1" })]}
        accountNames={{ "account-1": "Acme Corp" }}
      />
    );
    expect(html).toMatch(/Acme Corp/);
    expect(html).not.toMatch(/View in Accounts/);
  });

  it("renders a muted dash when accountId is null -- no fabricated value", () => {
    const html = renderToStaticMarkup(<ContactsTable items={[makeContact({ accountId: null })]} />);
    expect(html).not.toMatch(/View in Accounts/);
  });

  it("renders the real resolved owner name when ownerNames map has it", () => {
    const html = renderToStaticMarkup(
      <ContactsTable
        items={[makeContact({ ownerAgentId: "user-1" })]}
        ownerNames={{ "user-1": "Grace Hopper" }}
      />
    );
    expect(html).toMatch(/Grace Hopper/);
  });

  it("falls back to an em-dash when ownerAgentId is null -- never a fabricated name", () => {
    const html = renderToStaticMarkup(<ContactsTable items={[makeContact({ ownerAgentId: null })]} />);
    expect(html).toMatch(/—/);
  });

  it("does not render a Status column header (no `status` field on ContactResponse)", () => {
    const html = renderToStaticMarkup(<ContactsTable items={[makeContact()]} />);
    expect(html).not.toMatch(/>Status</);
  });

  it("renders exactly 5 column headers -- Name/Email/Company/Owner/Created, no checkbox column", () => {
    const html = renderToStaticMarkup(<ContactsTable items={[makeContact()]} />);
    const headerMatches = html.match(/<th\b/g) ?? [];
    expect(headerMatches).toHaveLength(5);
    expect(html).not.toMatch(/type="checkbox"/);
  });

  it("renders no sort icons and no filter funnels (G10: no backend sort/filter params)", () => {
    const html = renderToStaticMarkup(<ContactsTable items={[makeContact()]} />);
    expect(html).not.toMatch(/title="Sort"/);
    expect(html).not.toMatch(/title="Filter"/);
  });

  it("renders a 50-row list with no client-side per-row fetch (structural: the component takes items as a prop, no data hooks)", () => {
    const items = Array.from({ length: 50 }, (_, i) => makeContact({ contactId: `contact-${i}`, accountId: `account-${i}` }));
    const html = renderToStaticMarkup(<ContactsTable items={items} />);
    const matches = html.match(/View in Accounts/g) ?? [];
    expect(matches).toHaveLength(50);
  });
});
