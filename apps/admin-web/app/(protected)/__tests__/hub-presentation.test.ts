import { describe, expect, it } from "vitest";
import {
  answeredByChatbotRate,
  buildActivityRows,
  buildEscalationReading,
  buildGaugeReading,
  buildUsageBars,
  conversationInitials,
  conversationShortId,
  isHubBucket,
} from "@/app/(protected)/hub-presentation";
import { HUB_BUCKETS, HUB_PERIODS, type HubActiveConversation } from "@/lib/hub";
import type { AnalyticsOverview } from "@/lib/analytics";

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

/**
 * SR-16 D3/D2 data-mapping tests. Fixture where fallbackRate, deflectionRate
 * and groundedRate all differ, so the gauge/Conversation-Rate-card reading
 * `deflectionRate` specifically (not one of the other two) is asserted, not
 * assumed -- a mis-wire would fail this test even though all three fields
 * are superficially "a rate".
 */
function makeAnalytics(overrides: Partial<AnalyticsOverview> = {}): AnalyticsOverview {
  return {
    window: { from: "2026-07-01T00:00:00.000Z", to: "2026-07-08T00:00:00.000Z", bucket: "day" },
    totals: { conversations: 82, userTurns: 200, botTurns: 190, decidedBotTurns: 180 },
    intentDistribution: {},
    decisionDistribution: {},
    fallbackRate: 0.11, // deliberately distinct from deflectionRate/groundedRate
    deflectionRate: 0.72, // the ONE rate the gauge/Conversation Rate card must read
    groundedRate: 0.93, // deliberately distinct
    schedule: { ctaConversations: 10, conversions: 3, conversionRate: 0.3 },
    series: [],
    ...overrides,
  };
}

describe("answeredByChatbotRate / buildGaugeReading (SR-16 D3)", () => {
  it("reads deflectionRate specifically, not fallbackRate or groundedRate", () => {
    const analytics = makeAnalytics();
    expect(answeredByChatbotRate(analytics)).toBe(0.72);
    expect(answeredByChatbotRate(analytics)).not.toBe(analytics.fallbackRate);
    expect(answeredByChatbotRate(analytics)).not.toBe(analytics.groundedRate);
  });

  it("builds a rounded-percent reading with a real aria-label sentence", () => {
    const reading = buildGaugeReading(makeAnalytics({ deflectionRate: 0.594 }));
    expect(reading.rate).toBe(0.594);
    expect(reading.percent).toBe(59);
    expect(reading.label).toBe("Answered by chatbot: 59 percent");
  });

  it("NEVER renders a null deflectionRate as 0 percent -- the single highest-risk defect in this sprint", () => {
    const reading = buildGaugeReading(makeAnalytics({ deflectionRate: null }));
    expect(reading.rate).toBeNull();
    expect(reading.percent).toBeNull();
    expect(reading.label).toBe("Answered by chatbot: No data");
    expect(reading.label).not.toContain("0 percent");
    expect(reading.label).not.toContain("0%");
  });

  it("distinguishes a real 0% (all conversations escalated) from a null/undefined rate", () => {
    const zero = buildGaugeReading(makeAnalytics({ deflectionRate: 0 }));
    expect(zero.rate).toBe(0);
    expect(zero.percent).toBe(0);
    expect(zero.label).toBe("Answered by chatbot: 0 percent");

    const undefined_ = buildGaugeReading(makeAnalytics({ deflectionRate: null }));
    expect(undefined_.label).not.toBe(zero.label);
  });
});

describe("buildEscalationReading (SR-16 D4)", () => {
  it("derives escalation rate from series[] -- escalations / conversations, no new aggregate", () => {
    const analytics = makeAnalytics({
      series: [
        { bucketStart: "2026-07-01T00:00:00.000Z", conversations: 10, answers: 7, escalations: 3, bookings: 1 },
        { bucketStart: "2026-07-02T00:00:00.000Z", conversations: 10, answers: 9, escalations: 1, bookings: 0 },
      ],
    });
    const reading = buildEscalationReading(analytics);
    expect(reading.escalations).toBe(4);
    expect(reading.handled).toBe(16);
    expect(reading.rate).toBeCloseTo(0.2);
    expect(reading.percent).toBe(20);
  });

  it("renders No data (null rate) for a window with zero conversations -- never a fabricated 0%", () => {
    const analytics = makeAnalytics({
      series: [
        { bucketStart: "2026-07-01T00:00:00.000Z", conversations: 0, answers: 0, escalations: 0, bookings: 0 },
      ],
    });
    const reading = buildEscalationReading(analytics);
    expect(reading.rate).toBeNull();
    expect(reading.percent).toBeNull();
    expect(reading.label).toBe("Escalation rate: No data");
  });

  it("renders a true, honest 0% when there ARE conversations but zero escalations (not the same as null)", () => {
    const analytics = makeAnalytics({
      series: [
        { bucketStart: "2026-07-01T00:00:00.000Z", conversations: 5, answers: 5, escalations: 0, bookings: 0 },
      ],
    });
    const reading = buildEscalationReading(analytics);
    expect(reading.rate).toBe(0);
    expect(reading.percent).toBe(0);
    expect(reading.label).toBe("Escalation rate: 0 percent");
  });

  it("handles an empty series array (no buckets at all) as No data, not a crash or a fabricated rate", () => {
    const reading = buildEscalationReading(makeAnalytics({ series: [] }));
    expect(reading.rate).toBeNull();
    expect(reading.escalations).toBe(0);
  });
});

describe("isHubBucket (SR-16 D4 -- day/week only, no month)", () => {
  it("accepts the real HUB_BUCKETS values", () => {
    expect(isHubBucket("day", HUB_BUCKETS)).toBe(true);
    expect(isHubBucket("week", HUB_BUCKETS)).toBe(true);
  });

  it("rejects 'month' even though the mockup implies more choices -- D4 is explicit about this", () => {
    expect(isHubBucket("month", HUB_BUCKETS)).toBe(false);
  });

  it("HUB_BUCKETS itself contains exactly day and week, nothing else", () => {
    expect(HUB_BUCKETS).toEqual(["day", "week"]);
  });
});
