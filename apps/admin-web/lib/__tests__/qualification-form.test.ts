import { describe, expect, it } from "vitest";
import { choicesToText, newKey, parseChoices } from "@/lib/qualification-form";

describe("qualification form helpers (plan 1.2)", () => {
  it("reads one 'label = points' choice per line", () => {
    expect(parseChoices("this month = 100\nin 1 to 3 months = 60\n\n later=20 ")).toEqual({
      ok: true,
      choices: [
        { label: "this month", points: 100 },
        { label: "in 1 to 3 months", points: 60 },
        { label: "later", points: 20 },
      ],
    });
  });

  it("keeps an '=' inside the label and uses the last one for points", () => {
    expect(parseChoices("a = b = 50\nno = 0")).toEqual({
      ok: true,
      choices: [
        { label: "a = b", points: 50 },
        { label: "no", points: 0 },
      ],
    });
  });

  it("explains what is wrong", () => {
    expect(parseChoices("yes = 100")).toEqual({ ok: false, error: "Give at least 2 answer choices." });
    expect(parseChoices("yes\nno = 0")).toEqual({ ok: false, error: 'Line "yes" needs "= points", e.g. "yes = 100".' });
    expect(parseChoices("yes = 120\nno = 0")).toEqual({ ok: false, error: 'Points for "yes" must be a whole number from 0 to 100.' });
    expect(parseChoices("yes = 1\nYes = 0")).toEqual({ ok: false, error: 'The choice "Yes" is listed twice.' });
  });

  it("round-trips choices to text", () => {
    const choices = [
      { label: "yes", points: 100 },
      { label: "no", points: 30 },
    ];
    expect(choicesToText(choices)).toBe("yes = 100\nno = 30");
    expect(parseChoices(choicesToText(choices))).toEqual({ ok: true, choices });
  });

  it("makes a stable key from the question that doesn't clash", () => {
    expect(newKey("What's your service area?", [])).toBe("whats_your_service_area");
    expect(newKey("What's your service area?", ["whats_your_service_area"])).toBe("whats_your_service_area_2");
    expect(newKey("???", [])).toBe("question");
    expect(newKey("1 big question", [])).toBe("q_1_big_question");
  });
});
