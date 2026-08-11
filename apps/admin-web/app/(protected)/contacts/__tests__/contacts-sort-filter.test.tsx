import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ContactsTable } from "@/app/(protected)/contacts/contacts-table";
import { ContactsFilter } from "@/app/(protected)/contacts/contacts-filter";

const items = [
  {
    contactId: "contact-1",
    accountId: "acc-1",
    leadId: null,
    name: "Jane Doe",
    email: "jane@example.com",
    phone: null,
    ownerAgentId: "agent-1",
    createdAt: "2026-07-01T00:00:00Z",
  },
];

describe("ContactsTable header sort links + Company filter funnel (SR-29)", () => {
  it("renders sort links for every sortable column", () => {
    const html = renderToStaticMarkup(
      <ContactsTable items={items} basePath="/contacts" currentParams={new URLSearchParams()} />
    );

    expect(html).toMatch(/aria-label="Sort Name ascending"/);
    expect(html).toMatch(/aria-label="Sort Email ascending"/);
    expect(html).toMatch(/aria-label="Sort Group by company ascending"/);
    expect(html).toMatch(/aria-label="Sort Owner ascending"/);
    expect(html).toMatch(/aria-label="Sort Created descending"/);
  });

  it("renders a filter funnel ONLY on the Company header, not Owner (dead-control guard)", () => {
    const html = renderToStaticMarkup(
      <ContactsTable items={items} basePath="/contacts" currentParams={new URLSearchParams()} />
    );

    expect(html).toMatch(/aria-label="Filter Company"/);
    expect(html).not.toMatch(/aria-label="Filter Owner"/);
    expect(html).not.toMatch(/aria-label="Filter Name"/);
    expect(html).not.toMatch(/aria-label="Filter Email"/);
    expect(html).not.toMatch(/aria-label="Filter Created"/);
  });

  it("the Company header uses the honest 'Group by company' label, not 'Sort by company'", () => {
    const html = renderToStaticMarkup(
      <ContactsTable items={items} basePath="/contacts" currentParams={new URLSearchParams()} />
    );

    expect(html).not.toMatch(/Sort by company/i);
    expect(html).toMatch(/Group by company/);
  });

  it("the Company filter funnel lists known accounts and marks the active one", () => {
    const html = renderToStaticMarkup(
      <ContactsTable
        items={items}
        basePath="/contacts"
        currentParams={new URLSearchParams("account_id=acc-1")}
        accountNames={{ "acc-1": "Acme Corp" }}
        accountIdFilter="acc-1"
      />
    );

    expect(html).toContain("Acme Corp");
    expect(html).toContain("All companies");
  });

  it("shows an honest truncation note when the account list is capped", () => {
    const html = renderToStaticMarkup(
      <ContactsTable
        items={items}
        basePath="/contacts"
        currentParams={new URLSearchParams()}
        accountNames={{ "acc-1": "Acme Corp" }}
        accountsTruncated
      />
    );

    expect(html).toMatch(/Showing the first 1 accounts/);
  });

  it("preserves the ?contact= drawer param but drops ?before= on a sort link (drop-params)", () => {
    const html = renderToStaticMarkup(
      <ContactsTable
        items={items}
        basePath="/contacts"
        currentParams={new URLSearchParams("contact=contact-1&before=2026-01-01")}
      />
    );

    const match = html.match(/href="([^"]*sort=name[^"]*)"/);
    expect(match).not.toBeNull();
    if (match) {
      const decoded = decodeURIComponent(match[1]);
      expect(decoded).not.toMatch(/contact=contact-1/);
      expect(decoded).not.toMatch(/before=/);
    }
  });
});

describe("ContactsFilter search box (SR-29)", () => {
  it("renders a search input with the current query preserved", () => {
    const html = renderToStaticMarkup(<ContactsFilter currentQuery="jane" currentAccountId={undefined} />);

    expect(html).toContain('name="q"');
    expect(html).toContain('value="jane"');
  });

  it("carries the active account_id as a hidden field so search doesn't drop the Company filter", () => {
    const html = renderToStaticMarkup(<ContactsFilter currentQuery={undefined} currentAccountId="acc-1" />);

    expect(html).toMatch(/type="hidden"[^>]*name="account_id"[^>]*value="acc-1"/);
  });
});
