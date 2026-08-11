import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ColumnSortLink } from "@/components/admin/column-sort-link";
import { LeadsTable } from "../leads-table";

describe("SR-25 lead table controls", () => {
  it("cycles an inactive column to its useful explicit default direction", () => {
    const markup = renderToStaticMarkup(
      <ColumnSortLink
        label="Score"
        sortKey="score"
        basePath="/leads"
        currentParams={new URLSearchParams("page=3&stage=qualified")}
        defaultDirection="desc"
        dropParams={["lead", "tab"]}
      />
    );

    expect(markup).toContain('href="/leads?stage=qualified&amp;sort=score&amp;dir=desc"');
    expect(markup).not.toContain("page=3");
  });

  it("cycles every sort through default, the opposite direction, then no sort", () => {
    const descendingScore = renderToStaticMarkup(
      <ColumnSortLink
        label="Score"
        sortKey="score"
        basePath="/leads"
        currentParams={new URLSearchParams("sort=score&dir=desc")}
        currentSort="score"
        currentDirection="desc"
        defaultDirection="desc"
        dropParams={["lead", "tab"]}
      />
    );
    const ascendingScore = renderToStaticMarkup(
      <ColumnSortLink
        label="Score"
        sortKey="score"
        basePath="/leads"
        currentParams={new URLSearchParams("sort=score&dir=asc")}
        currentSort="score"
        currentDirection="asc"
        defaultDirection="desc"
        dropParams={["lead", "tab"]}
      />
    );

    expect(descendingScore).toContain('href="/leads?sort=score&amp;dir=asc"');
    expect(ascendingScore).toContain('href="/leads"');
    expect(ascendingScore).not.toContain("sort=score");
  });

  it("renders all seven sortable headers and only the four real filter funnels", () => {
    const markup = renderToStaticMarkup(
      <LeadsTable
        items={[
          {
            leadId: "lead-1",
            name: null,
            email: null,
            phone: null,
            status: "new",
            stage: "captured",
            qualificationScore: null,
            assignedAgentId: null,
            source: "widget",
            createdAt: "2026-01-01T00:00:00Z",
          },
        ]}
        currentParams={new URLSearchParams("sort=score&dir=desc")}
        sort="score"
        direction="desc"
        currentRole="CLIENT_AGENT"
        currentUserId="agent-1"
      />
    );

    expect(markup.match(/aria-sort=/g) ?? []).toHaveLength(7);
    expect(markup.match(/aria-label="Filter /g) ?? []).toHaveLength(4);
    expect(markup).toContain('aria-sort="descending"');
    expect(markup).toContain("Assigned to me");
    expect(markup).not.toContain("Unassigned");
    expect(markup).toContain("overflow-visible");
  });

  it("does not offer a dead Assigned-to-me filter to client admins", () => {
    const markup = renderToStaticMarkup(
      <LeadsTable
        items={[]}
        currentParams={new URLSearchParams()}
        currentRole="CLIENT_ADMIN"
        currentUserId="admin-1"
        agents={[{ id: "agent-1", label: "Active Agent" }]}
      />
    );

    expect(markup).not.toContain("Assigned to me");
    expect(markup).toContain("Active Agent");
  });
});
