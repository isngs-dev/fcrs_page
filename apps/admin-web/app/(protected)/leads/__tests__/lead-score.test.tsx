import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("@/lib/outreach", () => ({ takeOver: vi.fn() }));

const { ScoreSection } = await import("@/app/(protected)/leads/lead-agents-tab");

describe("Lead score section (plan 1.2/1.3)", () => {
  it("shows the score, band, answers and what's still unknown", () => {
    const html = renderToStaticMarkup(
      <ScoreSection
        q={{
          score: 82,
          band: "high",
          answers: [
            {
              key: "start",
              question: "When would you like to get started?",
              choice: "this month",
              heard: "right away",
              channel: "call",
              at: "2026-11-17T10:00:00Z",
            },
          ],
          unanswered: ["Do you make the marketing decisions?"],
        }}
      />
    );
    expect(html).toContain(">82<");
    expect(html).toContain("High");
    expect(html).toContain("When would you like to get started?");
    expect(html).toContain("this month");
    expect(html).toContain("right away");
    expect(html).toContain("Still to learn: Do you make the marketing decisions?");
  });

  it("says when a lead isn't scored yet", () => {
    const html = renderToStaticMarkup(<ScoreSection q={{ score: null, band: null, answers: [], unanswered: [] }} />);
    expect(html).toContain("Not scored yet.");
  });
});
