import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SegmentTable } from "@/app/(protected)/outreach/performance/segment-table";
import { sourceLabel } from "@/lib/outreach-format";

const ROW = {
  key: "web_form",
  leads: 4,
  contacted: 4,
  meetings: 2,
  leadToMeeting: 0.5,
  qualified: 3,
  qualificationRate: 0.75,
  handedOff: 1,
  handoffRate: 0.25,
};

describe("performance split by source / campaign (plan 2.5)", () => {
  it("names lead sources plainly and keeps unknown ones readable", () => {
    expect(sourceLabel("web_form")).toBe("Website form");
    expect(sourceLabel("chat_identity")).toBe("Chat");
    expect(sourceLabel("booking")).toBe("Booking");
    expect(sourceLabel("meta_api")).toBe("Facebook");
    expect(sourceLabel("manual")).toBe("Added by hand / CSV");
    expect(sourceLabel("trade_show")).toBe("Trade show");
  });

  it("shows each segment's leads, meetings, qualified and hand-offs", () => {
    const html = renderToStaticMarkup(<SegmentTable title="By source" rows={[ROW]} label={sourceLabel} />);
    expect(html).toContain("By source");
    expect(html).toContain("Website form");
    expect(html).toContain(">4<");
    expect(html).toContain("2 (50%)");
    expect(html).toContain("3 (75%)");
    expect(html).toContain("1 (25%)");
  });

  it("says so when there is nothing to split", () => {
    const html = renderToStaticMarkup(<SegmentTable title="By campaign" rows={[]} />);
    expect(html).toContain("No leads in this period.");
  });
});
