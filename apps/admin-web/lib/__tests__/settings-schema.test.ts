import { describe, expect, it } from "vitest";
import {
  fieldValuesFromSettings,
  parseBusinessHours,
  parseSuggestedQuestions,
  settingsFormSchema,
  shouldResetFieldsToServerValues,
  stringifyBusinessHours,
  stringifySuggestedQuestions,
} from "@/lib/settings-schema";
import type { BotSettings } from "@/lib/settings";
import type { SaveState } from "@/app/(protected)/settings/actions";

describe("settingsFormSchema", () => {
  it("parses a fully valid input", () => {
    const result = settingsFormSchema.safeParse({
      greeting: "Hi there!",
      launcherLabel: "Chat with our team",
      sidebarWorkspaceLabel: "Acme support",
      dashboardTitle: "Support hub",
      escalationPolicy: "Escalate on refund requests.",
      tone: "friendly",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.greeting).toBe("Hi there!");
      expect(result.data.launcherLabel).toBe("Chat with our team");
      expect(result.data.sidebarWorkspaceLabel).toBe("Acme support");
      expect(result.data.dashboardTitle).toBe("Support hub");
      expect(result.data.escalationPolicy).toBe("Escalate on refund requests.");
      expect(result.data.tone).toBe("friendly");
    }
  });

  it("fails when greeting exceeds 2000 characters", () => {
    const result = settingsFormSchema.safeParse({
      greeting: "a".repeat(2001),
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "greeting")?.message;
      expect(msg).toMatch(/2000/);
    }
  });

  it("fails when escalationPolicy exceeds 2000 characters", () => {
    const result = settingsFormSchema.safeParse({
      greeting: "",
      escalationPolicy: "a".repeat(2001),
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "escalationPolicy")?.message;
      expect(msg).toMatch(/2000/);
    }
  });

  it("fails when tone exceeds 100 characters", () => {
    const result = settingsFormSchema.safeParse({
      greeting: "",
      escalationPolicy: "",
      tone: "a".repeat(101),
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "tone")?.message;
      expect(msg).toMatch(/100/);
    }
  });

  it("fails when launcherLabel exceeds 40 characters", () => {
    const result = settingsFormSchema.safeParse({
      greeting: "",
      launcherLabel: "a".repeat(41),
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "launcherLabel")?.message;
      expect(msg).toMatch(/40/);
    }
  });

  it("fails when either workspace label exceeds 80 characters", () => {
    for (const key of ["sidebarWorkspaceLabel", "dashboardTitle"] as const) {
      const result = settingsFormSchema.safeParse({
        greeting: "",
        launcherLabel: "",
        sidebarWorkspaceLabel: key === "sidebarWorkspaceLabel" ? "a".repeat(81) : "",
        dashboardTitle: key === "dashboardTitle" ? "a".repeat(81) : "",
        escalationPolicy: "",
        tone: "",
        businessHoursText: "",
        suggestedQuestionsText: "",
      });
      expect(result.success).toBe(false);
    }
  });

  it("fails when botName exceeds 40 characters", () => {
    const result = settingsFormSchema.safeParse({
      botName: "a".repeat(41),
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "botName")?.message;
      expect(msg).toMatch(/40/);
    }
  });

  it("accepts a blank accentColor (undefined) and a valid 6-digit hex", () => {
    const blank = settingsFormSchema.safeParse({
      accentColor: "",
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(blank.success).toBe(true);
    if (blank.success) expect(blank.data.accentColor).toBeUndefined();

    const valid = settingsFormSchema.safeParse({
      accentColor: "#16a34a",
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(valid.success).toBe(true);
    if (valid.success) expect(valid.data.accentColor).toBe("#16a34a");
  });

  it("rejects a malformed accentColor", () => {
    const result = settingsFormSchema.safeParse({
      accentColor: "blue",
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "accentColor")?.message;
      expect(msg).toMatch(/hex color/i);
    }
  });

  it("accepts a blank launcherPosition (undefined) and 'left'/'right'", () => {
    const blank = settingsFormSchema.safeParse({
      launcherPosition: "",
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(blank.success).toBe(true);
    if (blank.success) expect(blank.data.launcherPosition).toBeUndefined();

    const left = settingsFormSchema.safeParse({
      launcherPosition: "left",
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(left.success).toBe(true);
    if (left.success) expect(left.data.launcherPosition).toBe("left");
  });

  it("rejects an unrecognized launcherPosition", () => {
    const result = settingsFormSchema.safeParse({
      launcherPosition: "center",
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "launcherPosition")?.message;
      expect(msg).toMatch(/left or right/i);
    }
  });

  it("coerces blank optional fields to undefined (backend receives null, not '')", () => {
    const result = settingsFormSchema.safeParse({
      greeting: "   ",
      launcherLabel: "",
      sidebarWorkspaceLabel: "",
      dashboardTitle: "",
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.greeting).toBeUndefined();
      expect(result.data.launcherLabel).toBeUndefined();
      expect(result.data.escalationPolicy).toBeUndefined();
      expect(result.data.tone).toBeUndefined();
    }
  });
});

describe("parseBusinessHours", () => {
  it("blank/whitespace -> {ok:true, value:null}", () => {
    expect(parseBusinessHours("")).toEqual({ ok: true, value: null });
    expect(parseBusinessHours("   \n  ")).toEqual({ ok: true, value: null });
  });

  it("a valid JSON object -> {ok:true, value}", () => {
    const result = parseBusinessHours('{"mon": ["09:00", "17:00"]}');
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value).toEqual({ mon: ["09:00", "17:00"] });
    }
  });

  it("a JSON array -> {ok:false}", () => {
    const result = parseBusinessHours("[1,2]");
    expect(result.ok).toBe(false);
  });

  it("a JSON scalar (string) -> {ok:false}", () => {
    expect(parseBusinessHours('"5"').ok).toBe(false);
  });

  it("a JSON scalar (number) -> {ok:false}", () => {
    expect(parseBusinessHours("5").ok).toBe(false);
  });

  it("a JSON scalar (boolean) -> {ok:false}", () => {
    expect(parseBusinessHours("true").ok).toBe(false);
  });

  it("malformed JSON -> {ok:false}", () => {
    expect(parseBusinessHours("{").ok).toBe(false);
  });

  it("null literal -> {ok:false} (not a plain object)", () => {
    expect(parseBusinessHours("null").ok).toBe(false);
  });
});

describe("stringifyBusinessHours", () => {
  it("null/undefined -> empty string", () => {
    expect(stringifyBusinessHours(null)).toBe("");
    expect(stringifyBusinessHours(undefined)).toBe("");
  });

  it("round-trips through parseBusinessHours", () => {
    const value = { mon: ["09:00", "17:00"] };
    const str = stringifyBusinessHours(value);
    const parsed = parseBusinessHours(str);
    expect(parsed).toEqual({ ok: true, value });
  });
});

describe("parseSuggestedQuestions", () => {
  it("blank/whitespace -> {ok:true, value:null}", () => {
    expect(parseSuggestedQuestions("")).toEqual({ ok: true, value: null });
    expect(parseSuggestedQuestions("   \n  \n")).toEqual({ ok: true, value: null });
  });

  it("one question per non-blank line -> {ok:true, value}, blank lines dropped", () => {
    const result = parseSuggestedQuestions("Do you serve my area?\n\n  How much does it cost?  \n");
    expect(result).toEqual({
      ok: true,
      value: ["Do you serve my area?", "How much does it cost?"],
    });
  });

  it("more than 5 questions -> {ok:false}", () => {
    const result = parseSuggestedQuestions("a\nb\nc\nd\ne\nf");
    expect(result.ok).toBe(false);
  });

  it("a question over 200 characters -> {ok:false}", () => {
    const result = parseSuggestedQuestions("a".repeat(201));
    expect(result.ok).toBe(false);
  });

  it("exactly 5 questions of exactly 200 characters each -> {ok:true}", () => {
    const line = "a".repeat(200);
    const result = parseSuggestedQuestions([line, line, line, line, line].join("\n"));
    expect(result.ok).toBe(true);
  });
});

describe("stringifySuggestedQuestions", () => {
  it("null/undefined/empty array -> empty string", () => {
    expect(stringifySuggestedQuestions(null)).toBe("");
    expect(stringifySuggestedQuestions(undefined)).toBe("");
    expect(stringifySuggestedQuestions([])).toBe("");
  });

  it("round-trips through parseSuggestedQuestions", () => {
    const value = ["Do you serve my area?", "How much does it cost?"];
    const str = stringifySuggestedQuestions(value);
    expect(parseSuggestedQuestions(str)).toEqual({ ok: true, value });
  });
});

const baseSettings: BotSettings = {
  greeting: "Hi!",
  launcherLabel: "Chat with our team",
  sidebarWorkspaceLabel: "Acme support",
  dashboardTitle: "Support hub",
  botName: "Aria",
  accentColor: "#16a34a",
  launcherPosition: "left",
  suggestedQuestions: ["Do you serve my area?"],
  businessHours: { mon: ["09:00", "17:00"] },
  escalationPolicy: "Escalate on refunds.",
  tone: "friendly",
  answerThreshold: 0.7,
  escalateThreshold: 0.4,
  turnCap: 7,
  lowConfidenceStreakCap: 5,
  llmProvider: null,
  llmModel: null,
};

describe("fieldValuesFromSettings", () => {
  it("maps a fully-populated BotSettings to string field values", () => {
    expect(fieldValuesFromSettings(baseSettings)).toEqual({
      greeting: "Hi!",
      launcherLabel: "Chat with our team",
      sidebarWorkspaceLabel: "Acme support",
      dashboardTitle: "Support hub",
      botName: "Aria",
      accentColor: "#16a34a",
      launcherPosition: "left",
      suggestedQuestionsText: "Do you serve my area?",
      businessHoursText: '{\n  "mon": [\n    "09:00",\n    "17:00"\n  ]\n}',
      escalationPolicy: "Escalate on refunds.",
      tone: "friendly",
      turnCap: "7",
      lowConfidenceStreakCap: "5",
    });
  });

  it("maps nulls to empty strings (turnCap is never null -- always a number)", () => {
    expect(
      fieldValuesFromSettings({
        ...baseSettings,
        greeting: null,
        launcherLabel: null,
        sidebarWorkspaceLabel: null,
        dashboardTitle: null,
        businessHours: null,
        escalationPolicy: null,
        tone: null,
      })
    ).toEqual({
      greeting: "",
      launcherLabel: "",
      sidebarWorkspaceLabel: "",
      dashboardTitle: "",
      botName: "Aria",
      accentColor: "#16a34a",
      launcherPosition: "left",
      suggestedQuestionsText: "Do you serve my area?",
      businessHoursText: "",
      escalationPolicy: "",
      tone: "",
      turnCap: "7",
      lowConfidenceStreakCap: "5",
    });
  });
});

describe("settingsFormSchema -- turnCap", () => {
  it("a blank turnCap -> undefined (leave-as-is sentinel)", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      turnCap: "",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.turnCap).toBeUndefined();
    }
  });

  it("a valid whole-number turnCap parses to a number", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      turnCap: "3",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.turnCap).toBe(3);
    }
  });

  it("a non-numeric turnCap fails validation", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      turnCap: "not-a-number",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "turnCap")?.message;
      expect(msg).toMatch(/whole number/i);
    }
  });

  it("a turnCap below 1 fails validation", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      turnCap: "0",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find((i) => i.path[0] === "turnCap")?.message;
      expect(msg).toMatch(/at least 1/i);
    }
  });
});

describe("settingsFormSchema -- lowConfidenceStreakCap", () => {
  it("a blank lowConfidenceStreakCap -> undefined (leave-as-is sentinel)", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      lowConfidenceStreakCap: "",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.lowConfidenceStreakCap).toBeUndefined();
    }
  });

  it("a valid whole-number lowConfidenceStreakCap parses to a number", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      lowConfidenceStreakCap: "2",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.lowConfidenceStreakCap).toBe(2);
    }
  });

  it("a non-numeric lowConfidenceStreakCap fails validation", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      lowConfidenceStreakCap: "not-a-number",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find(
        (i) => i.path[0] === "lowConfidenceStreakCap"
      )?.message;
      expect(msg).toMatch(/whole number/i);
    }
  });

  it("a lowConfidenceStreakCap below 1 fails validation", () => {
    const result = settingsFormSchema.safeParse({
      escalationPolicy: "",
      tone: "",
      businessHoursText: "",
      suggestedQuestionsText: "",
      lowConfidenceStreakCap: "0",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const msg = result.error.issues.find(
        (i) => i.path[0] === "lowConfidenceStreakCap"
      )?.message;
      expect(msg).toMatch(/at least 1/i);
    }
  });
});

describe("shouldResetFieldsToServerValues", () => {
  // Reproduces the reported bug: two consecutive error states (attempt 1
  // failed, user edits, attempt 2 also failed) must NEVER trigger a reset --
  // the in-progress edit the user made between the two failed submissions
  // must survive.
  it("is false when transitioning from idle to idle", () => {
    const idle: SaveState = { status: "idle" };
    expect(shouldResetFieldsToServerValues(idle, idle)).toBe(false);
  });

  it("is false when transitioning from idle to an error state", () => {
    const idle: SaveState = { status: "idle" };
    const error: SaveState = {
      status: "error",
      fieldErrors: { businessHoursText: "must be valid JSON" },
      formError: null,
      correlationId: null,
    };
    expect(shouldResetFieldsToServerValues(idle, error)).toBe(false);
  });

  it("is false when transitioning from one error state to ANOTHER error state (the reported bug)", () => {
    const error1: SaveState = {
      status: "error",
      fieldErrors: { businessHoursText: "must be valid JSON" },
      formError: null,
      correlationId: null,
    };
    const error2: SaveState = {
      status: "error",
      fieldErrors: { businessHoursText: "must be valid JSON" },
      formError: null,
      correlationId: null,
    };
    expect(shouldResetFieldsToServerValues(error1, error2)).toBe(false);
  });

  it("is true when transitioning to a NEW saved state", () => {
    const error: SaveState = {
      status: "error",
      fieldErrors: {},
      formError: "Check the form and try again.",
      correlationId: null,
    };
    const saved: SaveState = { status: "saved", settings: baseSettings };
    expect(shouldResetFieldsToServerValues(error, saved)).toBe(true);
  });

  it("is false when the state object is referentially unchanged (no new transition, e.g. an unrelated parent re-render)", () => {
    const saved: SaveState = { status: "saved", settings: baseSettings };
    expect(shouldResetFieldsToServerValues(saved, saved)).toBe(false);
  });
});
