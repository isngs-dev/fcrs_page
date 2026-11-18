import { describe, expect, it } from "vitest";
import { OUTREACH_STATUSES } from "@/lib/outreach-format";

describe("lead statuses (plan 2.4)", () => {
  it("lists the whole journey in pipeline order, matching the API", () => {
    expect(OUTREACH_STATUSES.map((s) => s.value)).toEqual([
      "new",
      "contacted",
      "engaged",
      "qualified",
      "nurture",
      "meeting_booked",
      "no_show",
      "attended",
      "opportunity",
      "won",
      "lost",
      "disqualified",
      "human_handoff",
      "invalid_contact",
      "not_interested",
      "opted_out",
    ]);
  });

  it("gives the new stages plain labels", () => {
    const label = Object.fromEntries(OUTREACH_STATUSES.map((s) => [s.value, s.label]));
    expect(label.nurture).toBe("Nurture");
    expect(label.no_show).toBe("No-show");
    expect(label.attended).toBe("Attended");
    expect(label.opportunity).toBe("Opportunity");
    expect(label.won).toBe("Won");
    expect(label.lost).toBe("Lost");
    expect(label.disqualified).toBe("Disqualified");
  });
});
