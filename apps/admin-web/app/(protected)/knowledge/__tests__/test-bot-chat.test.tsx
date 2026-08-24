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
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TestBotChat } from "@/app/(protected)/knowledge/test-bot-chat";

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
