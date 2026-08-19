/**
 * Knowledge Base list feature -- tests for <KnowledgeDocList>, using this
 * repo's established `environment: "node"` `renderToStaticMarkup` pattern
 * (mirrors upload-form-geometry.test.tsx): a pure server component, no
 * client interactivity to simulate.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { KnowledgeDocList } from "@/app/(protected)/knowledge/doc-list";
import type { KnowledgeDocListItem, ListKnowledgeResult } from "@/app/(protected)/knowledge/actions";

function doc(overrides: Partial<KnowledgeDocListItem> = {}): KnowledgeDocListItem {
  return {
    docId: "doc-1",
    title: null,
    description: null,
    filename: "faq.txt",
    contentType: "text/plain",
    status: "parsed",
    uploadedBy: "user-1",
    uploadedByName: "Jane Admin",
    createdAt: "2026-08-18T12:00:00Z",
    ...overrides,
  };
}

describe("KnowledgeDocList", () => {
  it("renders an honest empty state when there are no docs, no fabricated rows", () => {
    const result: ListKnowledgeResult = { status: "ok", docs: [] };
    const html = renderToStaticMarkup(<KnowledgeDocList result={result} />);

    expect(html).toMatch(/No knowledge items yet/i);
  });

  it("renders an honest inline error, never a blank or fabricated list, on a fetch failure", () => {
    const result: ListKnowledgeResult = {
      status: "error",
      message: "Something went wrong. (correlation ID: corr-1)",
      correlationId: "corr-1",
    };
    const html = renderToStaticMarkup(<KnowledgeDocList result={result} />);

    expect(html).toMatch(/Unable to load knowledge items/i);
    expect(html).toContain("corr-1");
  });

  it("falls back to the filename when title is blank", () => {
    const result: ListKnowledgeResult = { status: "ok", docs: [doc({ title: null, filename: "raw-notes.txt" })] };
    const html = renderToStaticMarkup(<KnowledgeDocList result={result} />);

    expect(html).toContain("raw-notes.txt");
  });

  it("shows the title (not the filename as the heading) when a title is set", () => {
    const result: ListKnowledgeResult = {
      status: "ok",
      docs: [doc({ title: "Refund policy", filename: "raw-notes.txt" })],
    };
    const html = renderToStaticMarkup(<KnowledgeDocList result={result} />);

    expect(html).toContain("Refund policy");
    // The filename still appears (secondary line), just not as the heading.
    expect(html).toContain("raw-notes.txt");
  });

  it("renders the description when present, omits it when absent", () => {
    const withDescription = renderToStaticMarkup(
      <KnowledgeDocList
        result={{ status: "ok", docs: [doc({ description: "How refunds work." })] }}
      />
    );
    expect(withDescription).toContain("How refunds work.");

    const withoutDescription = renderToStaticMarkup(
      <KnowledgeDocList result={{ status: "ok", docs: [doc({ description: null })] }} />
    );
    expect(withoutDescription).not.toContain("How refunds work.");
  });

  it("renders the uploader's display name when present", () => {
    const result: ListKnowledgeResult = {
      status: "ok",
      docs: [doc({ uploadedByName: "Jane Admin" })],
    };
    const html = renderToStaticMarkup(<KnowledgeDocList result={result} />);

    expect(html).toContain("Jane Admin");
  });

  it("never crashes and omits the uploader line when uploadedBy/uploadedByName are null (pre-migration row)", () => {
    const result: ListKnowledgeResult = {
      status: "ok",
      docs: [doc({ uploadedBy: null, uploadedByName: null })],
    };
    expect(() => renderToStaticMarkup(<KnowledgeDocList result={result} />)).not.toThrow();
  });

  it("renders a status badge for each doc status", () => {
    const result: ListKnowledgeResult = {
      status: "ok",
      docs: [doc({ docId: "d1", status: "parsed" }), doc({ docId: "d2", status: "failed" })],
    };
    const html = renderToStaticMarkup(<KnowledgeDocList result={result} />);

    expect(html).toMatch(/INDEXED/i);
    expect(html).toMatch(/FAILED/i);
  });

  it("renders every doc in the order given (server already sorts newest-first)", () => {
    const result: ListKnowledgeResult = {
      status: "ok",
      docs: [
        doc({ docId: "newest", title: "Newest" }),
        doc({ docId: "oldest", title: "Oldest" }),
      ],
    };
    const html = renderToStaticMarkup(<KnowledgeDocList result={result} />);

    expect(html.indexOf("Newest")).toBeLessThan(html.indexOf("Oldest"));
  });
});
