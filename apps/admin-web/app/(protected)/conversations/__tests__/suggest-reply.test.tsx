/**
 * "Suggest a reply" -- tests for <SuggestReply>, using this repo's
 * established `environment: "node"` `renderToStaticMarkup` pattern (see
 * coverage-gaps.test.tsx's header comment for the full rationale). These
 * prove the INITIAL idle render (button present, no draft yet, no
 * network call made) -- clicking through to a loaded draft requires a real
 * browser and is covered by live verification, same as coverage-gaps'
 * teach-form submission.
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SuggestReply } from "@/app/(protected)/conversations/suggest-reply";
import type { SuggestReplyResult } from "@/lib/conversations";

describe("SuggestReply", () => {
  it("renders the 'Suggest a reply' button and makes no call before it's clicked", () => {
    const action = vi.fn<() => Promise<SuggestReplyResult>>();
    const html = renderToStaticMarkup(<SuggestReply suggestReplyAction={action} />);

    expect(html).toMatch(/Suggest a reply/);
    expect(action).not.toHaveBeenCalled();
  });

  it("renders no draft, error, or decision text before any interaction", () => {
    const action = vi.fn<() => Promise<SuggestReplyResult>>();
    const html = renderToStaticMarkup(<SuggestReply suggestReplyAction={action} />);

    expect(html).not.toContain("Copy draft");
    expect(html).not.toMatch(/confidence/i);
  });

  it("the button is not disabled in the idle state", () => {
    const action = vi.fn<() => Promise<SuggestReplyResult>>();
    const html = renderToStaticMarkup(<SuggestReply suggestReplyAction={action} />);

    expect(html).not.toContain('disabled=""');
  });
});
