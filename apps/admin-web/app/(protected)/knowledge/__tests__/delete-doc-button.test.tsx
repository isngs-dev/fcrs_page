/**
 * Structural test for `DeleteDocButton` (delete-a-knowledge-doc feature).
 * Renders via `renderToStaticMarkup` -- this repo's vitest config runs
 * `environment: "node"` (no DOM/RTL), so this only asserts the idle-state
 * markup shape, not the confirm/click interaction (covered by
 * `actions.test.ts` for `deleteKnowledgeDocAction` itself).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DeleteDocButton } from "@/app/(protected)/knowledge/delete-doc-button";

describe("DeleteDocButton", () => {
  it("renders a Delete trigger, not yet confirming", () => {
    const html = renderToStaticMarkup(<DeleteDocButton docId="doc-1" label="Pricing FAQ" />);

    expect(html).toMatch(/>Delete</);
    expect(html).not.toContain("Permanently delete");
  });

  it("does not render the confirm/cancel controls before the trigger is clicked", () => {
    const html = renderToStaticMarkup(<DeleteDocButton docId="doc-1" label="Pricing FAQ" />);

    expect(html).not.toMatch(/>Cancel</);
  });
});
