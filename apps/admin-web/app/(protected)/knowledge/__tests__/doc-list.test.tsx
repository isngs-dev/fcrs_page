/**
 * `KnowledgeDocList`'s `tenantId`-gating (platform-admin knowledge
 * redesign): with `tenantId`, each row renders via `<KnowledgeDocRow>`
 * (View/Export present); without it (the client-facing `/knowledge` call
 * site), the original plain card renders -- byte-for-byte unchanged, no
 * View/Export, no client-component mount.
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
  it("renders the plain card with no View/Export when tenantId is omitted (client-facing page, unchanged)", () => {
    const html = renderToStaticMarkup(<KnowledgeDocList result={okResult()} />);

    expect(html).toContain("Pricing FAQ");
    expect(html).not.toMatch(/>View</);
    expect(html).not.toMatch(/>Export</);
    expect(html).not.toContain("/knowledge/download/");
  });

  it("renders View/Export actions with a tenant-scoped export href when tenantId is passed (platform-admin)", () => {
    const html = renderToStaticMarkup(
      <KnowledgeDocList result={okResult()} tenantId="tenant-42" />
    );

    expect(html).toContain("Pricing FAQ");
    expect(html).toMatch(/>View</);
    expect(html).toMatch(/>Export</);
    expect(html).toContain("/knowledge/download/doc-1?tenant_id=tenant-42");
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
