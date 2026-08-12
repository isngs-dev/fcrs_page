import { describe, expect, it } from "vitest";
import {
  DEFAULT_WEEKLY_HOURS,
  availabilityFormSchema,
  parseWeeklyHours,
  serializeWeeklyHours,
} from "@/lib/availability-schema";

describe("availabilityFormSchema", () => {
  it("parses valid slot/buffer minutes to numbers", () => {
    const result = availabilityFormSchema.safeParse({
      timezone: "UTC",
      slotMinutes: "30",
      bufferMinutes: "0",
      weeklyHoursJson: "{}",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.slotMinutes).toBe(30);
      expect(result.data.bufferMinutes).toBe(0);
    }
  });

  it("fails on a blank timezone", () => {
    const result = availabilityFormSchema.safeParse({
      timezone: "",
      slotMinutes: "30",
      bufferMinutes: "0",
      weeklyHoursJson: "{}",
    });
    expect(result.success).toBe(false);
  });

  it("fails on a zero or non-numeric slot length", () => {
    for (const value of ["0", "abc", "-5", ""]) {
      const result = availabilityFormSchema.safeParse({
        timezone: "UTC",
        slotMinutes: value,
        bufferMinutes: "0",
        weeklyHoursJson: "{}",
      });
      expect(result.success).toBe(false);
    }
  });

  it("accepts a zero buffer but rejects a non-numeric one", () => {
    expect(
      availabilityFormSchema.safeParse({
        timezone: "UTC",
        slotMinutes: "30",
        bufferMinutes: "0",
        weeklyHoursJson: "{}",
      }).success
    ).toBe(true);
    expect(
      availabilityFormSchema.safeParse({
        timezone: "UTC",
        slotMinutes: "30",
        bufferMinutes: "abc",
        weeklyHoursJson: "{}",
      }).success
    ).toBe(false);
  });
});

describe("parseWeeklyHours", () => {
  it("the seeded default (Mon-Fri 09:00-18:00) round-trips through serialize/parse", () => {
    const raw = serializeWeeklyHours(DEFAULT_WEEKLY_HOURS);
    const result = parseWeeklyHours(raw);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value).toEqual({
        mon: [["09:00", "18:00"]],
        tue: [["09:00", "18:00"]],
        wed: [["09:00", "18:00"]],
        thu: [["09:00", "18:00"]],
        fri: [["09:00", "18:00"]],
      });
    }
  });

  it("rejects malformed JSON", () => {
    expect(parseWeeklyHours("{").ok).toBe(false);
  });

  it("rejects a JSON array", () => {
    expect(parseWeeklyHours("[1,2]").ok).toBe(false);
  });

  it("rejects an unknown day key", () => {
    const result = parseWeeklyHours(JSON.stringify({ funday: ["09:00", "18:00"] }));
    expect(result.ok).toBe(false);
  });

  it("rejects a window with start >= end", () => {
    const result = parseWeeklyHours(JSON.stringify({ mon: ["18:00", "09:00"] }));
    expect(result.ok).toBe(false);
  });

  it("rejects a non-HH:MM time", () => {
    const result = parseWeeklyHours(JSON.stringify({ mon: ["9am", "18:00"] }));
    expect(result.ok).toBe(false);
  });

  it("rejects an all-closed week (every day null)", () => {
    const result = parseWeeklyHours(
      JSON.stringify({ mon: null, tue: null, wed: null, thu: null, fri: null, sat: null, sun: null })
    );
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toMatch(/at least one day/i);
    }
  });
});
