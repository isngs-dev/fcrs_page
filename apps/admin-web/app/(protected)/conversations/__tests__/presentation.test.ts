import { describe, expect, it } from "vitest";
import {
  formatDateTime,
  initialsFromVisitor,
  lastActivityAt,
  relativeTime,
  statusBadgeStyle,
  visitorLabel,
} from "@/app/(protected)/conversations/presentation";

describe("statusBadgeStyle", () => {
  it("maps the real 'active' status to an honest ACTIVE badge (never LIVE) using the reference success pair + 6x6px dot color (SR-27 slice 2)", () => {
    expect(statusBadgeStyle("active")).toEqual({
      label: "ACTIVE",
      bg: "#eaf3ec",
      fg: "#3f7d57",
      dot: "#4a9c6d",
    });
  });

  it("maps the real 'ended' status to an ENDED badge with no dot (SR-15 monochrome restyle)", () => {
    expect(statusBadgeStyle("ended")).toEqual({ label: "ENDED", bg: "#efeee6", fg: "var(--ink-2)" });
  });

  it("falls back to a neutral style for an unrecognized status rather than throwing", () => {
    const style = statusBadgeStyle("bogus");
    expect(style.label).toBe("BOGUS");
    expect(style.bg).toBe("#efeee6");
  });
});

describe("initialsFromVisitor", () => {
  it("takes the first two characters of the visitor id", () => {
    expect(initialsFromVisitor("4821")).toBe("48");
  });

  it("falls back to '?' for a null visitor id", () => {
    expect(initialsFromVisitor(null)).toBe("?");
  });

  it("falls back to '?' for a blank visitor id rather than throwing", () => {
    expect(initialsFromVisitor("   ")).toBe("?");
  });
});

describe("visitorLabel", () => {
  it("labels a known visitor id", () => {
    expect(visitorLabel("4821")).toBe("Visitor 4821");
  });

  it("labels a null visitor id as anonymous rather than fabricating a name", () => {
    expect(visitorLabel(null)).toBe("Anonymous visitor");
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-07-19T12:00:00Z");

  it("returns 'just now' for under a minute", () => {
    expect(relativeTime("2026-07-19T11:59:30Z", now)).toBe("just now");
  });

  it("returns minutes for under an hour", () => {
    expect(relativeTime("2026-07-19T11:58:00Z", now)).toBe("2m ago");
  });

  it("returns hours for under a day", () => {
    expect(relativeTime("2026-07-19T09:00:00Z", now)).toBe("3h ago");
  });

  it("returns days for a day or more", () => {
    expect(relativeTime("2026-07-17T12:00:00Z", now)).toBe("2d ago");
  });

  it("falls back to the raw string for an unparseable timestamp", () => {
    expect(relativeTime("not-a-date", now)).toBe("not-a-date");
  });
});

describe("lastActivityAt", () => {
  it("prefers endedAt when the conversation has ended", () => {
    expect(
      lastActivityAt({ startedAt: "2026-07-19T00:00:00Z", endedAt: "2026-07-19T00:10:00Z" })
    ).toBe("2026-07-19T00:10:00Z");
  });

  it("falls back to startedAt when the conversation is still active", () => {
    expect(lastActivityAt({ startedAt: "2026-07-19T00:00:00Z", endedAt: null })).toBe(
      "2026-07-19T00:00:00Z"
    );
  });
});

describe("formatDateTime", () => {
  it("falls back to the raw string for an unparseable timestamp", () => {
    expect(formatDateTime("not-a-date")).toBe("not-a-date");
  });

  it("formats a valid ISO timestamp without throwing", () => {
    expect(() => formatDateTime("2026-07-19T12:00:00Z")).not.toThrow();
    expect(formatDateTime("2026-07-19T12:00:00Z")).not.toBe("2026-07-19T12:00:00Z");
  });
});
