/**
 * Train the Agent -- tests for <CoverageGaps>, using this repo's established
 * `environment: "node"` `renderToStaticMarkup` pattern (no jsdom/testing-
 * library in this codebase -- see pipeline-board.test.tsx's header comment
 * for the full rationale). These tests prove the INITIAL rendered structure
 * (empty state, error state, populated rows with question/decision/
 * confidence) -- submitting the inline teach form requires a real browser
 * and is covered by this session's live Browser-pane verification instead.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CoverageGaps } from "@/app/(protected)/knowledge/coverage-gaps";
import type { CoverageGapItem, ListGapsResult } from "@/app/(protected)/knowledge/actions";

function gap(overrides: Partial<CoverageGapItem> = {}): CoverageGapItem {
  return {
    messageId: "m1",
    question: "How much does an inspection cost?",
    questionMessageId: "q1",
    decision: "escalate",
    confidence: 0.1,
    createdAt: "2026-08-18T12:00:00Z",
    ...overrides,
  };
}

describe("CoverageGaps", () => {
  it("renders an honest empty state when there are no gaps, no fabricated rows", () => {
    const result: ListGapsResult = { status: "ok", gaps: [] };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).toMatch(/No gaps right now/i);
  });

  it("renders an honest inline error, never a blank or fabricated list, on a fetch failure", () => {
    const result: ListGapsResult = {
      status: "error",
      message: "Something went wrong. (correlation ID: corr-1)",
      correlationId: "corr-1",
    };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).toMatch(/Unable to load coverage gaps/i);
    expect(html).toContain("corr-1");
  });

  it("renders each gap's question and decision", () => {
    const result: ListGapsResult = {
      status: "ok",
      gaps: [gap({ question: "How much does an inspection cost?", decision: "escalate" })],
    };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).toContain("How much does an inspection cost?");
    expect(html).toMatch(/escalate/i);
  });

  it("renders confidence as a rounded percentage when present", () => {
    const result: ListGapsResult = { status: "ok", gaps: [gap({ confidence: 0.42 })] };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).toContain("42%");
  });

  it("omits a confidence percentage when confidence is null", () => {
    const result: ListGapsResult = { status: "ok", gaps: [gap({ confidence: null })] };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).not.toContain("%");
  });

  it("renders every gap in the order given (server already sorts newest-first)", () => {
    const result: ListGapsResult = {
      status: "ok",
      gaps: [gap({ messageId: "m1", question: "Newest question" }), gap({ messageId: "m2", question: "Oldest question" })],
    };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html.indexOf("Newest question")).toBeLessThan(html.indexOf("Oldest question"));
  });

  it("renders a teach form (textarea + save button) for each gap", () => {
    const result: ListGapsResult = { status: "ok", gaps: [gap()] };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).toContain("<textarea");
    expect(html).toMatch(/Save answer/i);
  });

  it("renders a dismiss action (\"not a real question\") alongside the teach form for each gap", () => {
    const result: ListGapsResult = { status: "ok", gaps: [gap({ question: "I will not answer that" })] };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).toContain("I will not answer that");
    expect(html).toMatch(/Not a real question/i);
  });

  it("renders a 'Suggest a reply' button (not disabled) alongside the teach form for each gap", () => {
    const result: ListGapsResult = { status: "ok", gaps: [gap()] };
    const html = renderToStaticMarkup(<CoverageGaps result={result} />);

    expect(html).toMatch(/Suggest a reply/i);
    const suggestButton = html.slice(0, html.indexOf("Suggest a reply"));
    const lastButtonStart = suggestButton.lastIndexOf("<button");
    expect(suggestButton.slice(lastButtonStart)).not.toContain('data-disabled=""');
  });
});
