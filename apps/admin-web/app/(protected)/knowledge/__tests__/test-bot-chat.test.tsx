/**
 * Train the Agent -- tests for <TestBotChat>, using this repo's established
 * `environment: "node"` `renderToStaticMarkup` pattern (no jsdom/testing-
 * library -- see pipeline-board.test.tsx's header comment). `TestBotChat`
 * starts with an empty, purely client-side turn list (nothing is persisted
 * server-side), so there is no props-driven state to vary here -- these
 * tests prove the INITIAL structure only. Sending a message, rendering a
 * reply, and the inline teach form appearing on a non-answer decision all
 * require a real browser and are covered by this session's live
 * Browser-pane verification instead.
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TeachForm, TestBotChat } from "@/app/(protected)/knowledge/test-bot-chat";

describe("TestBotChat", () => {
  it("renders the heading, an honest empty-chat state, and the input", () => {
    const html = renderToStaticMarkup(<TestBotChat />);

    expect(html).toMatch(/Test the bot/i);
    expect(html).toContain("Ask a question to preview the answer");
    expect(html).toMatch(/Run test/i);
  });

  it("starts with no chat turns rendered (nothing persisted, fresh load = fresh chat)", () => {
    const html = renderToStaticMarkup(<TestBotChat />);

    // No turn list yet -- only the intro copy + input, no <ul> of turns.
    expect(html).not.toContain("<ul");
  });

  it("renders the input disabled=false and the send button starts disabled (no text yet)", () => {
    const html = renderToStaticMarkup(<TestBotChat />);

    expect(html).toMatch(/<button[^>]*disabled[^>]*>[\s\S]*Run test/);
  });
});

/**
 * <TeachForm> only ever renders inside <TestBotChat> once a non-answer turn
 * exists (client-side state, unreachable from a static initial render), so
 * it's exported and tested directly here -- same rationale as
 * coverage-gaps.tsx's teach form. Submitting either button (fills the
 * textarea from a draft / saves the answer) requires a real browser and is
 * covered by live verification instead, same as the rest of this file.
 */
describe("TeachForm", () => {
  it("renders both the 'Suggest a reply' and 'Save answer' buttons up front", () => {
    const html = renderToStaticMarkup(
      <TeachForm question="What areas do you serve?" onTaught={vi.fn()} />
    );

    expect(html).toMatch(/Suggest a reply/i);
    expect(html).toMatch(/Save answer/i);
  });

  it("starts with an empty textarea (placeholder, no value) and a disabled Save button", () => {
    const html = renderToStaticMarkup(
      <TeachForm question="What areas do you serve?" onTaught={vi.fn()} />
    );

    expect(html).toContain("What should the bot say to this question?");
    const saveButton = html.slice(0, html.indexOf("Save answer"));
    expect(saveButton.slice(saveButton.lastIndexOf("<button"))).toContain('data-disabled=""');
  });

  it("the 'Suggest a reply' button is not disabled up front (no draft in flight)", () => {
    const html = renderToStaticMarkup(
      <TeachForm question="What areas do you serve?" onTaught={vi.fn()} />
    );

    const suggestButton = html.slice(0, html.indexOf("Suggest a reply"));
    expect(suggestButton).not.toContain('data-disabled=""');
  });
});
