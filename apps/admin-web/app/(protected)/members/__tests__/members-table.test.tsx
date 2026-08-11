/**
 * Geometry/structural fidelity tests for the Team members table restyle
 * (visual pass against Console.dc.html:333-358), using this repo's
 * established `environment: "node"` `renderToStaticMarkup` pattern (no
 * jsdom -- see `contacts-table.test.tsx`). Asserts real rendered output,
 * not just that the code compiles.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { MemberSummary } from "@/lib/members";
import { MembersTable } from "@/app/(protected)/members/members-table";

function makeMember(overrides: Partial<MemberSummary> = {}): MemberSummary {
  return {
    id: "user-1",
    tenantId: "tenant-1",
    email: "ada@example.com",
    role: "CLIENT_AGENT",
    name: "Ada Lovelace",
    active: true,
    lastLoginAt: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

describe("MembersTable (visual/structural rebuild)", () => {
  it("renders exactly five column headers -- no checkbox column, no dead controls beyond sort", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember()]} />);
    // Member / Role / Last active / Status / Actions(sr-only)
    const headerMatches = html.match(/<th\b/g) ?? [];
    expect(headerMatches).toHaveLength(5);
  });

  it("does not render a leading checkbox column (no bulk-mutation backend)", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember()]} />);
    expect(html).not.toMatch(/type="checkbox"/);
  });

  it("does not render filter-funnel icons (no backend filter param on GET /admin/users)", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember()]} />);
    expect(html).not.toMatch(/title="Filter"/);
    expect(html).not.toMatch(/aria-label="Filter/);
  });

  it("renders a client-side sort control on Member, Role, and Last active headers", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember()]} />);
    expect(html).toMatch(/aria-label="Sort Member ascending"/);
    expect(html).toMatch(/aria-label="Sort Role ascending"/);
    expect(html).toMatch(/aria-label="Sort Last active ascending"/);
  });

  it("does not render a sort control on the Status column (low-value binary sort, skipped)", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember()]} />);
    expect(html).not.toMatch(/aria-label="Sort Status/);
  });

  it("H1/subtitle/table all render the reference's real member data, not fabricated fields", () => {
    const html = renderToStaticMarkup(
      <MembersTable members={[makeMember({ name: null, email: "solo@example.com" })]} />
    );
    expect(html).toMatch(/solo@example\.com/);
  });

  it("renders the Active status chip with visible text, not color-only", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember({ active: true })]} />);
    expect(html).toMatch(/>Active</);
  });

  it("renders the Inactive status with visible text for a deactivated member", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember({ active: false })]} />);
    expect(html).toMatch(/>Inactive</);
    expect(html).toMatch(/>Activate</);
  });

  it("renders a Deactivate button (.btn-outline.btn-sm geometry: h-8/32px, rounded-[9px], 12.5px text) for an active member", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember({ active: true })]} />);
    expect(html).toMatch(/>Deactivate</);
    expect(html).toMatch(/h-8/);
    expect(html).toMatch(/rounded-\[9px\]/);
    expect(html).toMatch(/text-\[12\.5px\]/);
  });

  it("renders the role chip with the real 2-role label (ADMIN/AGENT), not a fabricated third tier", () => {
    const adminHtml = renderToStaticMarkup(
      <MembersTable members={[makeMember({ role: "CLIENT_ADMIN" })]} />
    );
    expect(adminHtml).toMatch(/>ADMIN</);

    const agentHtml = renderToStaticMarkup(
      <MembersTable members={[makeMember({ role: "CLIENT_AGENT" })]} />
    );
    expect(agentHtml).toMatch(/>AGENT</);
  });

  it("renders a colgroup with 5 columns proportioned from the reference's 44px 2fr 150px 170px 130px 130px template (checkbox column dropped)", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember()]} />);
    const colMatches = html.match(/<col\b/g) ?? [];
    expect(colMatches).toHaveLength(5);
  });

  it("renders a 32px (h-8 w-8) avatar", () => {
    const html = renderToStaticMarkup(<MembersTable members={[makeMember()]} />);
    expect(html).toMatch(/h-8 w-8/);
  });
});
