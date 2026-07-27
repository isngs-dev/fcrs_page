import { describe, expect, it } from "vitest";
import { clampSidebarWidth } from "@/components/admin/admin-shell";

describe("desktop sidebar sizing", () => {
  it("clamps drag widths to the documented desktop bounds", () => {
    expect(clampSidebarWidth(120)).toBe(200);
    expect(clampSidebarWidth(248)).toBe(248);
    expect(clampSidebarWidth(480)).toBe(360);
  });
});
