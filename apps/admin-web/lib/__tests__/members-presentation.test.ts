import { describe, expect, it, vi } from "vitest";
import {
  formatLastActive,
  initialsFromMember,
  roleBadgeStyle,
} from "@/lib/members-presentation";

describe("roleBadgeStyle", () => {
  it("maps CLIENT_ADMIN to the ink/white badge (SR-15: citron deleted per D1)", () => {
    expect(roleBadgeStyle("CLIENT_ADMIN")).toEqual({
      label: "ADMIN",
      bg: "#333333",
      fg: "#ffffff",
    });
  });

  it("maps CLIENT_AGENT to the SR-15 monochrome badge", () => {
    expect(roleBadgeStyle("CLIENT_AGENT")).toEqual({
      label: "AGENT",
      bg: "#efeee6",
      fg: "#404040",
    });
  });

  it("falls back to a neutral style for an unknown role rather than throwing", () => {
    expect(roleBadgeStyle("PLATFORM_ADMIN")).toEqual({
      label: "PLATFORM_ADMIN",
      bg: "#ecece5",
      fg: "var(--ink-2)",
    });
  });
});

describe("initialsFromMember", () => {
  it("uses the first letters of a two-word name", () => {
    expect(initialsFromMember("Sara Romero", "sara@acme.studio")).toBe("SR");
  });

  it("falls back to the email local-part when name is null", () => {
    expect(initialsFromMember(null, "dev@acme.studio")).toBe("DE");
  });

  it("falls back to the email local-part when name is blank", () => {
    expect(initialsFromMember("   ", "tia@acme.studio")).toBe("TI");
  });

  it("returns ? when both name and email local-part are empty", () => {
    expect(initialsFromMember(null, "@acme.studio")).toBe("?");
  });
});

describe("formatLastActive", () => {
  it("returns 'Never logged in' for null", () => {
    expect(formatLastActive(null)).toBe("Never logged in");
  });

  it("returns 'Never logged in' for an unparsable timestamp", () => {
    expect(formatLastActive("not-a-date")).toBe("Never logged in");
  });

  it("returns 'Just now' for timestamps under a minute old", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T12:00:00Z"));
    expect(formatLastActive("2026-07-19T11:59:45Z")).toBe("Just now");
    vi.useRealTimers();
  });

  it("returns minutes ago for under an hour", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T12:00:00Z"));
    expect(formatLastActive("2026-07-19T11:48:00Z")).toBe("12 min ago");
    vi.useRealTimers();
  });

  it("returns hours ago for under a day", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T12:00:00Z"));
    expect(formatLastActive("2026-07-19T09:00:00Z")).toBe("3 h ago");
    vi.useRealTimers();
  });

  it("returns Yesterday for exactly one day ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T12:00:00Z"));
    expect(formatLastActive("2026-07-18T12:00:00Z")).toBe("Yesterday");
    vi.useRealTimers();
  });

  it("returns days ago for under a week", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-19T12:00:00Z"));
    expect(formatLastActive("2026-07-15T12:00:00Z")).toBe("4 days ago");
    vi.useRealTimers();
  });
});
