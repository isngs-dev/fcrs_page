import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AccountsTable } from "@/app/(protected)/accounts/accounts-table";
import { AccountsFilter } from "@/app/(protected)/accounts/accounts-filter";

const items = [
  { accountId: "acct-1", name: "Acme Corp", domain: "acme.example", createdAt: "2026-07-01T00:00:00Z" },
];

describe("AccountsTable header sort links (SR-29)", () => {
  it("renders sort links for Name and Domain on the shared 'Account' header, and Created", () => {
    const html = renderToStaticMarkup(
      <AccountsTable items={items} basePath="/accounts" currentParams={new URLSearchParams()} />
    );

    expect(html).toMatch(/aria-label="Sort Name ascending"/);
    expect(html).toMatch(/aria-label="Sort Domain ascending"/);
    expect(html).toMatch(/aria-label="Sort Created descending"/);
  });

  it("renders NO filter funnel anywhere on this page (D-FILTER-ACCOUNTS)", () => {
    const html = renderToStaticMarkup(
      <AccountsTable items={items} basePath="/accounts" currentParams={new URLSearchParams()} />
    );

    expect(html).not.toMatch(/Filter (Account|Domain|Created|Name)/);
    expect(html).not.toContain("<details");
  });

  it("sets aria-sort on the active column's header cell", () => {
    const html = renderToStaticMarkup(
      <AccountsTable
        items={items}
        basePath="/accounts"
        currentParams={new URLSearchParams("sort=created&dir=desc")}
        sort="created"
        direction="desc"
      />
    );

    expect(html).toMatch(/aria-sort="descending"[^>]*>[^<]*<span[^>]*>[^<]*<span[^>]*>Created/);
  });

  it("a sort link resets the page param", () => {
    const html = renderToStaticMarkup(
      <AccountsTable
        items={items}
        basePath="/accounts"
        currentParams={new URLSearchParams("page=3")}
      />
    );

    const match = html.match(/href="([^"]*sort=name[^"]*)"/);
    expect(match).not.toBeNull();
    if (match) {
      expect(decodeURIComponent(match[1])).not.toMatch(/page=3/);
    }
  });
});

describe("AccountsFilter search box (SR-29)", () => {
  it("renders a search input with the current query preserved", () => {
    const html = renderToStaticMarkup(<AccountsFilter currentQuery="acme" />);

    expect(html).toContain('name="q"');
    expect(html).toContain('value="acme"');
  });

  it("renders a Clear link only when a query is active", () => {
    const withQuery = renderToStaticMarkup(<AccountsFilter currentQuery="acme" />);
    const withoutQuery = renderToStaticMarkup(<AccountsFilter currentQuery={undefined} />);

    expect(withQuery).toMatch(/Clear/);
    expect(withoutQuery).not.toMatch(/Clear/);
  });
});
