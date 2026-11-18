/**
 * Structural test for `DocReviewControl` (plan 2.6) -- node environment, so markup only;
 * the PATCH itself is covered by `reviewKnowledgeDocAction` in actions.test.ts.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DocReviewControl } from "@/app/(protected)/knowledge/doc-review-control";

describe("DocReviewControl", () => {
  it("shows the document's category selected and every category on offer", () => {
    const html = renderToStaticMarkup(<DocReviewControl docId="d1" category="pricing" approved />);

    expect(html).toContain('<option value="pricing" selected="">Pricing</option>');
    expect(html).toContain('value="sales_playbook"');
    expect(html).toContain('value="general"');
  });

  it("says whether the agent uses the document, and offers the switch", () => {
    const on = renderToStaticMarkup(<DocReviewControl docId="d1" category="faq" approved />);
    const off = renderToStaticMarkup(<DocReviewControl docId="d1" category="faq" approved={false} />);

    expect(on).toContain("Used by the agent");
    expect(on).toMatch(/>Stop using</);
    expect(off).toContain("Not used by the agent");
    expect(off).toMatch(/>Use it</);
  });
});
