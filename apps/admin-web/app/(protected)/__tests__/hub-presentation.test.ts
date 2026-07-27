import { describe, expect, it } from "vitest";
import {
  buildActivityRows,
  buildUsageBars,
  conversationInitials,
  conversationShortId,
} from "@/app/(protected)/hub-presentation";
import { HUB_PERIODS, type HubActiveConversation } from "@/lib/hub";

const KNOWN_FABRICATED_NAMES = ["Tahsan", "Khan", "James", "Willey", "Azam"];

function makeConversation(overrides: Partial<HubActiveConversation> = {}): HubActiveConversation {
  return {
    conversationId: "conv-9f2a",
    status: "active",
    channel: "widget",
    startedAt: "2026-07-20T10:00:00.000Z",
    messageCount: 3,
    summary: "Asked about pricing",
    ...overrides,
  };
}

const stubFormatDate = (iso: string) => `formatted(${iso})`;

describe("conversationShortId / conversationInitials", () => {
  it("derives a C-<last4> short id from the real conversation id (never a name)", () => {
    expect(conversationShortId("conversation-9f2a")).toBe("C-9F2A");
  });

  it("derives 2-char initials from the id short form, not from any person name", () => {
    expect(conversationInitials("conversation-9f2a")).toBe("9F");
  });

  it("falls back safely for a blank id rather than throwing or inventing content", () => {
    expect(conversationShortId("   ")).toBe("C-????");
    expect(conversationInitials("   ")).toBe("C");
  });
});

describe("buildActivityRows", () => {
  it("builds up to 3 rows from the real active conversations", () => {
    const rows = buildActivityRows(
      [
        makeConversation({ conversationId: "conv-0001" }),
        makeConversation({ conversationId: "conv-0002" }),
        makeConversation({ conversationId: "conv-0003" }),
      ],
      stubFormatDate
    );
    expect(rows).toHaveLength(3);
    expect(rows[0]?.identityLabel).toBe("C-0001");
  });

  it("never pads to 3 -- 0 or 1 real items yield 0 or 1 rows", () => {
    expect(buildActivityRows([], stubFormatDate)).toHaveLength(0);
    expect(buildActivityRows([makeConversation()], stubFormatDate)).toHaveLength(1);
  });

  it("uses the real summary, falling back to `Started <date>` when summary is null", () => {
    const [withSummary] = buildActivityRows([makeConversation({ summary: "Real summary" })], stubFormatDate);
    expect(withSummary?.preview).toBe("Real summary");

    const [noSummary] = buildActivityRows(
      [makeConversation({ summary: null, startedAt: "2026-07-20T10:00:00.000Z" })],
      stubFormatDate
    );
    expect(noSummary?.preview).toBe("Started formatted(2026-07-20T10:00:00.000Z)");
  });

  it("treats a whitespace-only summary as empty (falls back, does not render blank)", () => {
    const [row] = buildActivityRows([makeConversation({ summary: "   " })], stubFormatDate);
    expect(row?.preview).toBe("Started formatted(2026-07-20T10:00:00.000Z)");
  });

  it("NEVER produces a fabricated person name -- every field traces to a real HubActiveConversation field", () => {
    const rows = buildActivityRows(
      [
        makeConversation({ conversationId: "conv-aaaa", summary: "Question about hours" }),
        makeConversation({ conversationId: "conv-bbbb", summary: null }),
      ],
      stubFormatDate
    );
    const serialized = JSON.stringify(rows);
    for (const name of KNOWN_FABRICATED_NAMES) {
      expect(serialized).not.toContain(name);
    }
    // Every row's identity content is derived from the real conversationId.
    for (const row of rows) {
      expect(row.identityLabel.startsWith("C-")).toBe(true);
      expect(row.initials.length).toBeLessThanOrEqual(2);
      expect(["active"]).toContain(row.status);
    }
  });
});

describe("buildUsageBars", () => {
  it("fills only the selected period's bar (single real total), leaving others empty -- no fabricated numbers", () => {
    const bars = buildUsageBars("month", 82, HUB_PERIODS);
    expect(bars).toHaveLength(HUB_PERIODS.length);
    const month = bars.find((b) => b.period === "month");
    expect(month?.selected).toBe(true);
    expect(month?.fill).toBe(1);
    for (const bar of bars.filter((b) => !b.selected)) {
      expect(bar.fill).toBe(0);
    }
  });

  it("shows an empty selected bar when the real total is 0 (honest zero, not a fabricated fill)", () => {
    const bars = buildUsageBars("week", 0, HUB_PERIODS);
    const week = bars.find((b) => b.period === "week");
    expect(week?.selected).toBe(true);
    expect(week?.fill).toBe(0);
  });

  it("never assigns a non-zero fill to a period whose total was not fetched", () => {
    const bars = buildUsageBars("year", 100, HUB_PERIODS);
    const nonSelectedFills = bars.filter((b) => !b.selected).map((b) => b.fill);
    expect(nonSelectedFills.every((fill) => fill === 0)).toBe(true);
  });
});
