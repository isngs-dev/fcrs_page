/**
 * `KnowledgeDocList`'s `tenantId`-gating (platform-admin knowledge
 * redesign): with `tenantId`, each row renders via `<KnowledgeDocRow>`
 * (View/Export present, no Delete -- platform admins stay read-only);
 * without it (the client-facing `/knowledge` call site), the plain card
 * renders with a `<DeleteDocButton>` (delete-a-knowledge-doc feature) and
 * no View/Export.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { KnowledgeDocList } from "@/app/(protected)/knowledge/doc-list";
import type { ListKnowledgeResult } from "@/app/(protected)/knowledge/actions";

function okResult(): ListKnowledgeResult {
  return {
    status: "ok",
    docs: [
      {
        docId: "doc-1",
        title: "Pricing FAQ",
        description: "Common pricing questions.",
        filename: "pricing.txt",
        contentType: "text/plain",
        status: "parsed",
        uploadedBy: "user-1",
        uploadedByName: "Jane Doe",
        createdAt: "2026-01-01T00:00:00Z",
      },
    ],
  };
}

describe("KnowledgeDocList", () => {
  it("renders the plain card with a Delete button but no View/Export when tenantId is omitted (client-facing page)", () => {
    const html = renderToStaticMarkup(<KnowledgeDocList result={okResult()} />);

    expect(html).toContain("Pricing FAQ");
    expect(html).toMatch(/>Delete</);
    expect(html).not.toMatch(/>View</);
    expect(html).not.toMatch(/>Export</);
    expect(html).not.toContain("/knowledge/download/");
  });

  it("renders View/Export actions with a tenant-scoped export href when tenantId is passed (platform-admin), never Delete", () => {
    const html = renderToStaticMarkup(
      <KnowledgeDocList result={okResult()} tenantId="tenant-42" />
    );

    expect(html).toContain("Pricing FAQ");
    expect(html).toMatch(/>View</);
    expect(html).toMatch(/>Export</);
    expect(html).toContain("/knowledge/download/doc-1?tenant_id=tenant-42");
    expect(html).not.toMatch(/>Delete</);
  });

  it("still shows the honest error/empty states regardless of tenantId", () => {
    const errorHtml = renderToStaticMarkup(
      <KnowledgeDocList
        result={{ status: "error", message: "boom", correlationId: "c1" }}
        tenantId="tenant-42"
      />
    );
    expect(errorHtml).toMatch(/role="alert"/);

    const emptyHtml = renderToStaticMarkup(
      <KnowledgeDocList result={{ status: "ok", docs: [] }} tenantId="tenant-42" />
    );
    expect(emptyHtml).toMatch(/No knowledge items yet/);
  });
});
