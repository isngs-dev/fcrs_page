import { describe, expect, it } from "vitest";
import {
  clampSidebarWidth,
  isNavParent,
  navGroups,
  parentContainsActivePath,
  splitMobileNavItems,
  visibleGroupsForRole,
  visibleItemsForRole,
  visibleLeafHrefsForRole,
  type NavItem,
  type NavParent,
} from "@/components/admin/admin-shell";

describe("desktop sidebar sizing", () => {
  it("clamps drag widths to the documented desktop bounds", () => {
    expect(clampSidebarWidth(120)).toBe(200);
    expect(clampSidebarWidth(248)).toBe(248);
    expect(clampSidebarWidth(480)).toBe(360);
  });
});

// SR-15 D6/M6: RBAC-aware nav rendering, asserted per role -- not "an admin
// sees a sidebar". The nav filter (visibleGroupsForRole) is UI
// defense-in-depth only; the backend's own require_roles(...) remains the
// real authorization boundary (admin-web skill) -- these tests assert what
// the sidebar *shows*, never that a role *can* reach data because a link is
// hidden or shown.
describe("SR-30 D30-1: nested Overview nav (supersedes SR-15 D6's flat-nav clause; Deals still soft-disabled)", () => {
  it("CLIENT_ADMIN can reach Contacts/Accounts from the nav (now nested under Leads), and no Deals entry", () => {
    const hrefs = visibleLeafHrefsForRole("CLIENT_ADMIN");
    expect(hrefs).toContain("/contacts");
    expect(hrefs).toContain("/accounts");
    expect(hrefs).not.toContain("/deals");
  });

  it("CLIENT_AGENT can reach the same two entries too (D6's role table, unchanged by SR-30), and no Deals entry", () => {
    const hrefs = visibleLeafHrefsForRole("CLIENT_AGENT");
    expect(hrefs).toContain("/contacts");
    expect(hrefs).toContain("/accounts");
    expect(hrefs).not.toContain("/deals");
  });

  it("PLATFORM_ADMIN sees NONE of the CRM entries at any nesting level, and still sees /clients + /clients/new", () => {
    const hrefs = visibleLeafHrefsForRole("PLATFORM_ADMIN");
    expect(hrefs).not.toContain("/contacts");
    expect(hrefs).not.toContain("/accounts");
    expect(hrefs).not.toContain("/deals");
    expect(hrefs).not.toContain("/analytics");
    expect(hrefs).not.toContain("/reports");
    expect(hrefs).toContain("/clients");
    expect(hrefs).toContain("/clients/new");
  });

  it("'Add a chatbot' is nested under the Clients parent as a real, PLATFORM_ADMIN-only destination", () => {
    const platformGroup = navGroups.find((group) => group.label === "Platform")!;
    const clients = platformGroup.items.find((item) => item.href === "/clients")!;
    expect(isNavParent(clients)).toBe(true);
    const child = (clients as NavParent).children.find((c) => c.href === "/clients/new");
    expect(child?.label).toBe("Add a chatbot");
    expect(child?.roles).toEqual(["PLATFORM_ADMIN"]);
  });

  it("labels the third entity 'Accounts', never 'Companies' (D-naming; D30-2 re-affirms this despite the screenshot)", () => {
    const accountsItem = navGroups
      .flatMap((group) => group.items)
      .flatMap((item) => (isNavParent(item) ? [item, ...item.children] : [item]))
      .find((item) => item.href === "/accounts");
    expect(accountsItem?.label).toBe("Accounts");
    expect(accountsItem?.label).not.toMatch(/companies/i);
  });

  it("nests Analytics/Reports under Dashboard and Contacts/Accounts under Leads (D30-1), leaving Conversations flat", () => {
    const overviewGroup = navGroups.find((group) => group.label === "Overview")!;
    expect(overviewGroup.items.map((item) => item.href)).toEqual([
      "/",
      "/conversations",
      "/leads",
    ]);

    const dashboard = overviewGroup.items.find((item) => item.href === "/")!;
    expect(isNavParent(dashboard)).toBe(true);
    expect((dashboard as NavParent).children.map((c) => c.href)).toEqual([
      "/analytics",
      "/reports",
    ]);

    const leads = overviewGroup.items.find((item) => item.href === "/leads")!;
    expect(isNavParent(leads)).toBe(true);
    expect((leads as NavParent).children.map((c) => c.href)).toEqual([
      "/contacts",
      "/accounts",
    ]);

    const conversations = overviewGroup.items.find((item) => item.href === "/conversations")!;
    expect(isNavParent(conversations)).toBe(false);
  });

  it("D30-3: nesting does NOT change any child's URL", () => {
    const hrefs = visibleLeafHrefsForRole("CLIENT_ADMIN");
    for (const href of ["/analytics", "/reports", "/contacts", "/accounts"]) {
      expect(hrefs).toContain(href);
    }
  });

  it("moves Analytics/Reports OUT of the Tools group (they are children of Dashboard now)", () => {
    const toolsGroup = navGroups.find((group) => group.label === "Tools");
    expect(toolsGroup?.items.map((item) => item.href)).toEqual([
      "/notifications",
      "/knowledge",
      "/settings",
      "/workspace",
    ]);
  });

  it("CLIENT_ADMIN's filtered Leads parent keeps both children after RBAC filtering, and no Deals", () => {
    const overviewGroup = visibleGroupsForRole("CLIENT_ADMIN").find(
      (group) => group.label === "Overview"
    )!;
    const leads = overviewGroup.items.find((item) => item.href === "/leads")!;
    expect(isNavParent(leads)).toBe(true);
    const childHrefs = (leads as NavParent).children.map((c) => c.href);
    expect(childHrefs).toContain("/contacts");
    expect(childHrefs).toContain("/accounts");
    expect(childHrefs).not.toContain("/deals");
  });

  it("PLATFORM_ADMIN's filtered groups contain no Overview group at all (no CRM entries leak in)", () => {
    const groups = visibleGroupsForRole("PLATFORM_ADMIN");
    expect(groups.find((group) => group.label === "Overview")).toBeUndefined();
    expect(groups.map((group) => group.label)).toContain("Platform");
  });
});

// M6 non-regression: appending to overviewItems must not have widened
// /knowledge or /members's existing CLIENT_ADMIN-only gate.
describe("SR-15 non-regression: existing role gates are untouched", () => {
  it("/knowledge remains CLIENT_ADMIN-only after the restyle", () => {
    expect(visibleItemsForRole("CLIENT_ADMIN").map((i) => i.href)).toContain("/knowledge");
    expect(visibleItemsForRole("CLIENT_AGENT").map((i) => i.href)).not.toContain("/knowledge");
  });

  it("/members remains CLIENT_ADMIN-only after the restyle", () => {
    expect(visibleItemsForRole("CLIENT_ADMIN").map((i) => i.href)).toContain("/members");
    expect(visibleItemsForRole("CLIENT_AGENT").map((i) => i.href)).not.toContain("/members");
  });

  // Workspace settings split out of the combined "/settings" route on user
  // request -- see app/(protected)/workspace/page.tsx's doc comment. That
  // shell (General/Members/Billing/API keys/Notifications/Danger zone) was
  // always CLIENT_ADMIN-only even while it lived inside "/settings", so the
  // sidebar gate mirrors /knowledge and /members above, not "/settings"'s
  // two-role gate.
  it("/workspace is CLIENT_ADMIN-only; /settings (Bot settings) stays reachable by both roles", () => {
    expect(visibleItemsForRole("CLIENT_ADMIN").map((i) => i.href)).toContain("/workspace");
    expect(visibleItemsForRole("CLIENT_AGENT").map((i) => i.href)).not.toContain("/workspace");
    expect(visibleItemsForRole("CLIENT_ADMIN").map((i) => i.href)).toContain("/settings");
    expect(visibleItemsForRole("CLIENT_AGENT").map((i) => i.href)).toContain("/settings");
  });
});

// D5's whole point: the mobile bottom-nav overflow math is a real
// behavioral consequence of D6 (grew CLIENT_ADMIN's nav from six items;
// Deals was later soft-disabled, so the count is 8, not the original 9) --
// SR-30 D30-4 keeps children out of this feed, so the count is now 6, not 8.
// The exact count is intentionally not hardcoded here, only asserted to
// exceed MOBILE_NAV_MAX, per the spec's own instruction to assert behavior,
// not a magic number that drifts with every nav-entry change.
describe("SR-15 D5/D6: mobile nav overflow exercised by CLIENT_ADMIN's grown nav", () => {
  it("CLIENT_ADMIN's visible items split into 4 primary + the remainder overflowed (MOBILE_NAV_MAX=5)", () => {
    const items = visibleItemsForRole("CLIENT_ADMIN");
    expect(items.length).toBeGreaterThan(5);
    const { primary, overflow } = splitMobileNavItems(items);
    expect(primary).toHaveLength(4);
    expect(overflow.length).toBe(items.length - 4);
  });

  it("D30-4: the mobile feed carries PARENTS only -- nested children are desktop-only", () => {
    const hrefs = visibleItemsForRole("CLIENT_ADMIN").map((item) => item.href);
    expect(hrefs).toContain("/");
    expect(hrefs).toContain("/leads");
    expect(hrefs).not.toContain("/contacts");
    expect(hrefs).not.toContain("/accounts");
    expect(hrefs).not.toContain("/analytics");
    expect(hrefs).not.toContain("/reports");
  });

  it("a role with <= MOBILE_NAV_MAX items shows everything with no overflow", () => {
    const items = visibleItemsForRole("PLATFORM_ADMIN");
    const { primary, overflow } = splitMobileNavItems(items);
    expect(primary).toEqual(items);
    expect(overflow).toHaveLength(0);
  });

  it("splits exactly at the boundary (max items -> no overflow, max+1 -> overflow of 2)", () => {
    const fakeItems: NavItem[] = Array.from({ length: 5 }, (_, i) => ({
      href: `/x${i}`,
      label: `X${i}`,
      icon: () => null,
      roles: ["CLIENT_ADMIN"],
    }));
    expect(splitMobileNavItems(fakeItems, 5).overflow).toHaveLength(0);
    const sixItems: NavItem[] = [
      ...fakeItems,
      { href: "/x5", label: "X5", icon: () => null, roles: ["CLIENT_ADMIN"] },
    ];
    const { primary, overflow } = splitMobileNavItems(sixItems, 5);
    expect(primary).toHaveLength(4);
    expect(overflow).toHaveLength(2);
  });
});

describe("SR-30 D30-7: the active child auto-expands its parent", () => {
  const overview = navGroups.find((g) => g.label === "Overview")!;
  const dashboard = overview.items.find((i) => i.href === "/") as NavParent;
  const leads = overview.items.find((i) => i.href === "/leads") as NavParent;

  it("visiting /reports expands Dashboard, not Leads", () => {
    expect(parentContainsActivePath(dashboard, "/reports")).toBe(true);
    expect(parentContainsActivePath(leads, "/reports")).toBe(false);
  });

  it("visiting a nested sub-route (/reports/funnel) still expands Dashboard", () => {
    expect(parentContainsActivePath(dashboard, "/reports/funnel")).toBe(true);
  });

  it("visiting /accounts expands Leads, not Dashboard", () => {
    expect(parentContainsActivePath(leads, "/accounts")).toBe(true);
    expect(parentContainsActivePath(dashboard, "/accounts")).toBe(false);
  });

  it("a parent's own route does not by itself mark a child active", () => {
    expect(parentContainsActivePath(leads, "/leads")).toBe(false);
  });
});
