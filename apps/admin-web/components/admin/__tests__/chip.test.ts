/**
 * D4 (success is never color-only) pure-logic coverage. `Chip` itself is a
 * React component (no DOM renderer in this repo's test environment -- see
 * design-system.test.ts's header comment), so this file tests the contract
 * data every `<Chip tone="success">` call site in the restyled pages is
 * built from: the badge-style lookup tables always pair a color with a
 * real text `label`, never colour alone.
 */
import { describe, expect, it } from "vitest";
import { stageBadgeStyle, scoreChipStyle } from "@/lib/leads-presentation";
import { statusBadgeStyle } from "@/app/(protected)/conversations/presentation";

describe("SR-15 D4: every badge/chip style carries a real text label", () => {
  it("stageBadgeStyle never returns an empty label for any canonical stage", () => {
    for (const stage of ["captured", "qualified", "contacted", "converted", "disqualified"]) {
      expect(stageBadgeStyle(stage).label.trim().length).toBeGreaterThan(0);
    }
  });

  it("the converted stage (this table's success case) uses the system's success color pair", () => {
    expect(stageBadgeStyle("converted")).toEqual({
      label: "CONVERTED",
      bg: "#eaf3ec",
      fg: "#3f7d57",
    });
  });

  it("scoreChipStyle never returns an empty label", () => {
    expect(scoreChipStyle(75, "qualified").label).toBe("75");
    expect(scoreChipStyle(90, "converted").label).toBe("90");
  });

  it("conversations statusBadgeStyle's active state uses the system's success color pair with a real label", () => {
    const active = statusBadgeStyle("active");
    expect(active.label).toBe("ACTIVE");
    // SR-27 slice 2: `dot` (Console.dc.html:394's 6x6px status dot color)
    // was added to the badge-style contract for the reference chip
    // geometry -- `active`'s dot is the same success-green as its fg.
    expect(active).toEqual({ label: "ACTIVE", bg: "#eaf3ec", fg: "#3f7d57", dot: "#4a9c6d" });
  });

  it("an unrecognized status still returns a non-empty label (no-silent-fallback)", () => {
    const unknown = statusBadgeStyle("weird_status");
    expect(unknown.label.trim().length).toBeGreaterThan(0);
  });
});
