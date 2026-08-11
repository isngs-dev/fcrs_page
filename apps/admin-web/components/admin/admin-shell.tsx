"use client";

import { useState, useSyncExternalStore, type PointerEvent as ReactPointerEvent } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BarChart3,
  Bell,
  BookOpen,
  Building2,
  ChevronDown,
  Contact,
  FileBarChart,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Settings2,
  Users2,
  UsersRound,
} from "lucide-react";
import type { Role } from "@/lib/auth";
import { cn } from "@/lib/utils";

interface AdminShellProps {
  children: React.ReactNode;
  role: Role;
  identityLabel: string;
  identityRoleLabel?: string;
  sidebarWorkspaceLabel?: string | null;
  sidebarStorageScope?: string;
  logoutAction: () => Promise<void>;
}

/** A leaf navigation entry -- a real route. Unchanged from SR-15 except that
 *  it is now also usable as a child of a NavParent (SR-30 D30-1). */
export interface NavItem {
  href: string;
  label: string;
  mobileLabel?: string;
  // SR-23: `strokeWidth` is part of the contract now -- the reference's
  // `.sb-link svg` is explicitly `stroke-width:1.8`, not lucide's 2 default.
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean; strokeWidth?: number }>;
  roles: Role[];
}

/** SR-30 D30-1: a nav entry that is BOTH a destination and a collapsible
 *  parent of leaf entries. `children` is non-empty by construction; an entry
 *  with no children stays a plain NavItem. D30-5: `href` is real and
 *  navigable -- the disclosure toggle is a SEPARATE control. */
export interface NavParent extends NavItem {
  children: NavItem[];
}

/** Discriminate with the type guard below, never by `"children" in x`. */
export type NavEntry = NavItem | NavParent;

export function isNavParent(entry: NavEntry): entry is NavParent {
  return Array.isArray((entry as NavParent).children) && (entry as NavParent).children.length > 0;
}

/**
 * Nav groups mirror HANDOFF-SPEC.md's Sidebar recipe (Overview / Tools / Team).
 * PLATFORM_ADMIN sees a distinct "Platform" group instead, since Clients is a
 * different persona's workspace, not a peer of the client-facing tools.
 */
export interface NavGroup {
  label: string;
  items: NavEntry[]; // was NavItem[]
}

const overviewItems: NavEntry[] = [
  {
    href: "/",
    label: "Dashboard",
    icon: LayoutDashboard,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT"],
    children: [
      // Moved out of `toolsItems` by SR-30 D30-1. Roles copied verbatim from
      // their prior Tools entries (admin-shell.tsx pre-SR-30) --
      // CLIENT_ADMIN + CLIENT_AGENT, unchanged. D30-3: hrefs are UNCHANGED;
      // this is an IA change, not a routing change.
      { href: "/analytics", label: "Analytics", icon: BarChart3, roles: ["CLIENT_ADMIN", "CLIENT_AGENT"] },
      { href: "/reports", label: "Reports", icon: FileBarChart, roles: ["CLIENT_ADMIN", "CLIENT_AGENT"] },
    ],
  },
  {
    href: "/conversations",
    label: "Conversations",
    icon: MessageSquare,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT"],
  },
  {
    href: "/leads",
    label: "Leads",
    icon: UsersRound,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT"],
    children: [
      // SR-30 D30-1 SUPERSEDES SR-15 D6's "no nested sub-nav" clause for the
      // sidebar (user-approved reversal; see the SR-30 handoff section 0).
      // D6's OTHER clause -- no in-page segmented Leads/Contacts/Companies
      // pill -- is UNTOUCHED and still locked. Do not build the pill.
      // D30-2: this label is "Accounts", NEVER "Companies" (D-naming),
      // notwithstanding the target screenshot's wording.
      { href: "/contacts", label: "Contacts", icon: Contact, roles: ["CLIENT_ADMIN", "CLIENT_AGENT"] },
      { href: "/accounts", label: "Accounts", icon: Building2, roles: ["CLIENT_ADMIN", "CLIENT_AGENT"] },
    ],
  },
  // Deals nav entry remains soft-disabled (removed on user request, route and
  // components intentionally left on disk). Unchanged by SR-30.
];

const toolsItems: NavItem[] = [
  {
    href: "/notifications",
    label: "Notifications",
    icon: Bell,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT", "PLATFORM_ADMIN"],
  },
  {
    href: "/knowledge",
    label: "Knowledge base",
    mobileLabel: "Knowledge",
    icon: BookOpen,
    roles: ["CLIENT_ADMIN"],
  },
  {
    href: "/settings",
    label: "Bot settings",
    mobileLabel: "Settings",
    icon: Settings2,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT"],
  },
];

const teamItems: NavItem[] = [
  {
    href: "/members",
    label: "Team members",
    mobileLabel: "Members",
    icon: Users2,
    roles: ["CLIENT_ADMIN"],
  },
];

const platformItems: NavItem[] = [
  {
    href: "/clients",
    label: "Clients",
    icon: Building2,
    roles: ["PLATFORM_ADMIN"],
  },
];

export const navGroups: NavGroup[] = [
  { label: "Overview", items: overviewItems },
  { label: "Tools", items: toolsItems },
  { label: "Team", items: teamItems },
  { label: "Platform", items: platformItems },
];

/** Pure role filter over `navGroups`. SR-30: now recurses one level into
 *  `children`. A parent whose own `roles` exclude the viewer is dropped WITH
 *  its children (a child is never promoted to top level -- promoting it would
 *  silently invent a nav shape no design specifies). A surviving parent keeps
 *  only the children the viewer may see; if every child is filtered out the
 *  parent survives as a plain leaf, because its own `href` is still a real
 *  destination the viewer is allowed to reach.
 *
 * Extracted so RBAC-aware nav correctness (D6/M6, SR-30 D30-1) is
 * unit-testable without rendering `AdminShell` (this repo's vitest config
 * runs with `environment: "node"`, no DOM/RTL -- see `vitest.config.ts` --
 * so pure-logic extraction is the established pattern here, matching
 * `clampSidebarWidth`'s existing test). Mirrors the component's own
 * `visibleGroups` derivation exactly.
 */
export function visibleGroupsForRole(role: Role): NavGroup[] {
  return navGroups
    .map((group) => ({
      label: group.label,
      items: group.items
        .filter((item) => item.roles.includes(role))
        .map((item) =>
          isNavParent(item)
            ? { ...item, children: item.children.filter((child) => child.roles.includes(role)) }
            : item
        ),
    }))
    .filter((group) => group.items.length > 0);
}

/** Flat list of every TOP-LEVEL nav entry a role can see, in registry order.
 *  SR-30 D30-4: children are deliberately NOT flattened in -- this feeds the
 *  mobile bottom nav, where a parent is the destination and its children are
 *  a desktop-only convenience. Use `visibleLeafHrefsForRole` when you need
 *  every reachable href including children. */
export function visibleItemsForRole(role: Role): NavEntry[] {
  return visibleGroupsForRole(role).flatMap((group) => group.items);
}

/** SR-30: every href a role can reach from the nav, parents AND children,
 *  in registry order. Exists so RBAC tests can assert reachability without
 *  depending on D30-4's mobile-shaped flattening. */
export function visibleLeafHrefsForRole(role: Role): string[] {
  return visibleGroupsForRole(role).flatMap((group) =>
    group.items.flatMap((item) =>
      isNavParent(item) ? [item.href, ...item.children.map((c) => c.href)] : [item.href]
    )
  );
}

function isCurrentPath(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

/** SR-30 D30-7: a parent auto-expands when it contains the active route, so
 *  visiting /reports never renders a sidebar that hides /reports. Exported so
 *  it is unit-testable in this repo's `environment: "node"` vitest setup
 *  (no DOM/RTL -- see admin-shell.test.ts for the convention). */
export function parentContainsActivePath(parent: NavParent, pathname: string): boolean {
  return parent.children.some((child) => isCurrentPath(pathname, child.href));
}

/**
 * Pure mobile-bottom-nav overflow split, extracted for unit testing (D5/D6
 * regression coverage): if there's room for every visible item within
 * `MOBILE_NAV_MAX` slots, show them all with no "More" link; otherwise
 * reserve the last slot for "More" and overflow the remainder into it.
 * SR-30 D30-4: parents-only feed this function (children are desktop-nested
 * only), which now caps CLIENT_ADMIN/CLIENT_AGENT at 6 top-level entries
 * (still > MOBILE_NAV_MAX, so the overflow split this function exists to
 * prove is still exercised -- see admin-shell.test.ts).
 */
export function splitMobileNavItems(
  items: NavEntry[],
  max: number = MOBILE_NAV_MAX
): { primary: NavEntry[]; overflow: NavEntry[] } {
  if (items.length <= max) {
    return { primary: items, overflow: [] };
  }
  return { primary: items.slice(0, max - 1), overflow: items.slice(max - 1) };
}

/** Mobile bottom nav shows at most this many items before overflowing into "More". */
const MOBILE_NAV_MAX = 5;
const SIDEBAR_MIN_WIDTH = 200;
const SIDEBAR_MAX_WIDTH = 360;
const SIDEBAR_DEFAULT_WIDTH = 248;
const SIDEBAR_COLLAPSED_WIDTH = 64;
const DEFAULT_SIDEBAR_PREFERENCES: SidebarPreferences = {
  width: SIDEBAR_DEFAULT_WIDTH,
  collapsed: false,
};
const sidebarPreferenceCache = new Map<string, { raw: string | null; value: SidebarPreferences }>();

interface SidebarPreferences {
  width: number;
  collapsed: boolean;
}

export function clampSidebarWidth(value: number): number {
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, value));
}

function sidebarStorageKey(scope: string | undefined): string {
  return `chatleads:admin-sidebar:v1:${scope ?? "default"}`;
}

function readSidebarPreferences(scope: string | undefined): SidebarPreferences {
  if (typeof window === "undefined") return DEFAULT_SIDEBAR_PREFERENCES;
  const key = sidebarStorageKey(scope);
  try {
    const raw = window.localStorage.getItem(key);
    const cached = sidebarPreferenceCache.get(key);
    if (cached?.raw === raw) return cached.value;
    const parsed = raw ? (JSON.parse(raw) as Partial<SidebarPreferences>) : {};
    const value = {
      width: typeof parsed.width === "number" ? clampSidebarWidth(parsed.width) : SIDEBAR_DEFAULT_WIDTH,
      collapsed: parsed.collapsed === true,
    };
    sidebarPreferenceCache.set(key, { raw, value });
    return value;
  } catch {
    return DEFAULT_SIDEBAR_PREFERENCES;
  }
}

function subscribeToSidebarPreferences(scope: string | undefined, callback: () => void): () => void {
  const key = sidebarStorageKey(scope);
  const eventName = `${key}:change`;
  const onStorage = (event: StorageEvent) => {
    if (event.key === key) callback();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener(eventName, callback);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(eventName, callback);
  };
}

function saveSidebarPreferences(scope: string | undefined, value: SidebarPreferences): void {
  const key = sidebarStorageKey(scope);
  try {
    const raw = JSON.stringify(value);
    window.localStorage.setItem(key, raw);
    sidebarPreferenceCache.set(key, { raw, value });
    window.dispatchEvent(new Event(`${key}:change`));
  } catch {
    // Local storage can be disabled; this session simply retains the default.
  }
}

/** SR-30: stable, non-empty DOM id for a parent's disclosure region. */
function slug(href: string): string {
  return href.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "") || "root";
}

/** Shared link body for a plain leaf, a parent row, or an indented child --
 *  extracted (SR-30 3.5) so all three share one implementation of the
 *  classNames/Icon block instead of three copies of the class string. */
function NavLink({
  item,
  current,
  collapsed,
  indent = false,
  className,
}: {
  item: NavItem;
  current: boolean;
  collapsed: boolean;
  indent?: boolean;
  className?: string;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-label={item.label}
      aria-current={current ? "page" : undefined}
      className={cn(
        "my-px flex items-center gap-[11px] rounded-[9px] px-[11px] py-2 font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring",
        indent ? "text-[13px]" : "text-[14px]",
        collapsed ? "mx-2 justify-center px-2" : indent ? "mx-2.5 ml-[38px]" : "mx-2.5",
        current
          ? "bg-border font-semibold text-foreground"
          : "text-[var(--ink-2)] hover:bg-[#e6e6e6] hover:text-foreground",
        className
      )}
    >
      {!indent ? (
        <Icon
          aria-hidden
          strokeWidth={1.8}
          className={cn("size-[17px] flex-none", current ? "text-foreground" : "text-muted-foreground")}
        />
      ) : null}
      <span className={collapsed ? "hidden" : undefined}>{item.label}</span>
    </Link>
  );
}

export function AdminShell({
  children,
  role,
  identityLabel,
  identityRoleLabel,
  sidebarWorkspaceLabel,
  sidebarStorageScope,
  logoutAction,
}: AdminShellProps) {
  const pathname = usePathname();
  const sidebar = useSyncExternalStore(
    (callback) => subscribeToSidebarPreferences(sidebarStorageScope, callback),
    () => readSidebarPreferences(sidebarStorageScope),
    () => DEFAULT_SIDEBAR_PREFERENCES
  );
  const [isResizingSidebar, setIsResizingSidebar] = useState(false);
  // SR-30 D30-6: expansion state is ephemeral and colocated here, deliberately
  // NOT part of `SidebarPreferences`/localStorage. Keyed by parent href;
  // `undefined` means "no explicit user choice yet" so D30-7's auto-expand
  // (`parentContainsActivePath`) can take over.
  const [explicitExpand, setExplicitExpand] = useState<Record<string, boolean>>({});

  function updateSidebar(updater: (current: SidebarPreferences) => SidebarPreferences) {
    saveSidebarPreferences(sidebarStorageScope, updater(sidebar));
  }

  function onResizeStart(event: ReactPointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    setIsResizingSidebar(true);
  }

  function onResizeMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!isResizingSidebar) return;
    updateSidebar((current) => ({
      ...current,
      collapsed: false,
      width: clampSidebarWidth(event.clientX),
    }));
  }

  function onResizeEnd(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setIsResizingSidebar(false);
  }
  const visibleGroups = visibleGroupsForRole(role);
  const visibleItems = visibleItemsForRole(role);
  const isPlatformAdmin = role === "PLATFORM_ADMIN";
  const roleLabel = identityRoleLabel ?? (isPlatformAdmin ? "Platform admin" : "Client workspace");
  const workspaceLabel =
    sidebarWorkspaceLabel?.trim() || (isPlatformAdmin ? "Platform workspace" : "Client workspace");

  // Cap the mobile bottom nav at MOBILE_NAV_MAX slots: if there's room for
  // every item, show them all; otherwise reserve the last slot for a "More"
  // overflow link to the first tool page rather than letting a 6th+ item
  // disappear or the row overflow off-screen.
  const { primary: mobilePrimaryItems, overflow: mobileOverflowItems } =
    splitMobileNavItems(visibleItems);
  const mobileSlotCount = mobilePrimaryItems.length + (mobileOverflowItems.length > 0 ? 1 : 0);

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <aside
        style={{ width: sidebar.collapsed ? SIDEBAR_COLLAPSED_WIDTH : sidebar.width }}
        className={cn(
          // SR-22 M9: reference Sidebar.dc.html:29 specifies `background:#ffffff`
          // (white), not the cream `--secondary` this previously used.
          // SR-23: horizontal padding moved OFF the <aside> and onto the nav
          // links themselves (`mx-2.5`, matching the reference's per-link
          // `margin:1px 10px`), so the logo/avatar blocks can carry the
          // reference's own `padding:0 20px` and their divider rules can span
          // the full 198px width as they do in Sidebar.dc.html:30/:64.
          "sticky top-0 hidden h-screen shrink-0 flex-col border-r border-border bg-card py-[18px] lg:flex",
          sidebar.collapsed && "items-center"
        )}
      >
        {/* Sidebar.dc.html:30-36 -- 34px mark + name/workspace, `padding:0 20px
            16px` with a full-width bottom rule. */}
        <div
          className={cn(
            "mb-1 flex items-center gap-[11px] border-b border-border px-5 pb-4",
            sidebar.collapsed && "w-full flex-col px-2"
          )}
        >
          <div className="grid size-[34px] flex-none place-items-center rounded-[9px] bg-primary text-xs font-bold text-primary-foreground">
            CL
          </div>
          <div className={cn("min-w-0 flex-1", sidebar.collapsed && "hidden")}>
            <p className="text-[14.5px] font-bold tracking-[-0.01em]">ChatLeads</p>
            <p className="truncate text-[11px] text-muted-foreground">
              {workspaceLabel}
            </p>
          </div>
          <button
            type="button"
            aria-label={sidebar.collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!sidebar.collapsed}
            onClick={() => updateSidebar((current) => ({ ...current, collapsed: !current.collapsed }))}
            className="grid size-8 shrink-0 place-items-center rounded-[9px] text-[var(--ink-2)] transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            {sidebar.collapsed ? <PanelLeftOpen aria-hidden className="size-4" /> : <PanelLeftClose aria-hidden className="size-4" />}
          </button>
        </div>

        {/* Sidebar.dc.html:14-26 -- `.sb-kicker` is `font-size:10.5px;
            letter-spacing:.12em; padding:0 21px; margin:16px 0 5px`, and
            `.sb-link` is `padding:8px 11px; margin:1px 10px; gap:11px;
            border-radius:9px; font-size:14px; font-weight:500` with a 17px
            stroke-1.8 icon, going `background:#cdcdcd; font-weight:600` when
            active and `#e6e6e6` on hover. */}
        <nav aria-label="Main navigation" className="flex w-full min-h-0 flex-1 flex-col overflow-y-auto">
          {visibleGroups.map((group) => (
            <div key={group.label} className="flex flex-col">
              <p
                className={cn(
                  "mt-4 mb-[5px] px-[21px] text-[10.5px] font-semibold tracking-[0.12em] text-muted-foreground uppercase",
                  sidebar.collapsed && "hidden"
                )}
              >
                {group.label}
              </p>
              {group.items.map((item) => {
                const current = isCurrentPath(pathname, item.href);
                if (!isNavParent(item)) {
                  return (
                    <NavLink
                      key={item.href}
                      item={item}
                      current={current}
                      collapsed={sidebar.collapsed}
                    />
                  );
                }

                // SR-30 D30-7: explicit user choice wins; absent one, a
                // parent auto-expands whenever the active route is one of
                // its children, so navigating straight to e.g. /reports
                // never renders a sidebar that hides /reports.
                const expanded =
                  explicitExpand[item.href] ?? parentContainsActivePath(item, pathname);
                const subId = `nav-sub-${slug(item.href)}`;
                return (
                  <div key={item.href} className="flex flex-col">
                    <div className="flex items-center">
                      <NavLink
                        item={item}
                        current={current}
                        collapsed={sidebar.collapsed}
                        className="flex-1"
                      />
                      {!sidebar.collapsed && (
                        <button
                          type="button"
                          aria-expanded={expanded}
                          aria-controls={subId}
                          aria-label={`${expanded ? "Collapse" : "Expand"} ${item.label} section`}
                          onClick={() =>
                            setExplicitExpand((prev) => ({ ...prev, [item.href]: !expanded }))
                          }
                          className="mr-2.5 grid size-6 shrink-0 place-items-center rounded-[7px] text-[var(--ink-2)] transition-colors hover:bg-[#e6e6e6] focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring"
                        >
                          <ChevronDown
                            aria-hidden
                            strokeWidth={1.8}
                            className={cn("size-3.5 transition-transform", !expanded && "-rotate-90")}
                          />
                        </button>
                      )}
                    </div>
                    {/* D30-8: children are hidden entirely in the 64px
                        collapsed rail -- no icons, no room, no hierarchy. */}
                    {!sidebar.collapsed && expanded && (
                      <div id={subId} className="flex flex-col">
                        {item.children.map((child) => (
                          <NavLink
                            key={child.href}
                            item={child}
                            current={isCurrentPath(pathname, child.href)}
                            collapsed={sidebar.collapsed}
                            indent
                          />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </nav>

        {/* Sidebar.dc.html:64-70 -- `margin-top:auto; padding:14px 20px 0`
            over a full-width top rule; 30px round `--line` avatar, 12.5px/600
            identity over an 11px muted role line. */}
        <div className="mt-auto w-full border-t border-border pt-3.5">
          <div className={cn("flex items-center gap-2.5 px-5", sidebar.collapsed && "justify-center px-2")}>
            <div className="grid size-[30px] flex-none place-items-center rounded-full bg-border text-[11px] font-semibold text-[var(--ink-2)]">
              {identityLabel.slice(0, 2).toUpperCase()}
            </div>
            <div className={cn("min-w-0 flex-1", sidebar.collapsed && "hidden")}>
              <p className="min-w-0 truncate text-[12.5px] font-semibold">{identityLabel}</p>
              <p className="min-w-0 truncate text-[11px] text-muted-foreground">{roleLabel}</p>
            </div>
          </div>
          <form action={logoutAction}>
            <button
              type="submit"
              aria-label="Log out"
              className={cn(
                "my-px mt-2 flex items-center gap-[11px] rounded-[9px] px-[11px] py-2 text-[14px] font-medium text-[var(--ink-2)] transition-colors hover:bg-[#e6e6e6] hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring",
                sidebar.collapsed ? "mx-2 justify-center px-2" : "mx-2.5 w-[calc(100%-20px)]"
              )}
            >
              <LogOut aria-hidden strokeWidth={1.8} className="size-[17px] flex-none text-muted-foreground" />
              <span className={sidebar.collapsed ? "hidden" : undefined}>Log out</span>
            </button>
          </form>
        </div>
        {!sidebar.collapsed ? (
          <div
            role="separator"
            aria-label="Resize sidebar"
            aria-orientation="vertical"
            onPointerDown={onResizeStart}
            onPointerMove={onResizeMove}
            onPointerUp={onResizeEnd}
            onPointerCancel={onResizeEnd}
            className="absolute top-0 right-[-5px] z-10 hidden h-full w-2 cursor-col-resize touch-none lg:block"
          />
        ) : null}
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex min-h-16 items-center border-b border-border bg-background px-4 lg:hidden">
          <Link href="/" className="flex items-center gap-2 text-sm font-bold">
            <span className="grid size-8 place-items-center rounded-[9px] bg-primary text-[10px] text-primary-foreground">
              CL
            </span>
            ChatLeads
          </Link>
          <form action={logoutAction} className="ml-auto">
            <button
              type="submit"
              aria-label="Log out"
              className="grid size-11 place-items-center rounded-[9px] text-[var(--ink-2)] transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <LogOut aria-hidden className="size-4" />
            </button>
          </form>
        </header>
        <div className="flex min-w-0 flex-1 flex-col pb-20 lg:pb-0">{children}</div>
        <nav
          aria-label="Mobile navigation"
          className="fixed inset-x-0 bottom-0 z-40 grid min-h-16 border-t border-border bg-background/95 px-2 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden"
          style={{ gridTemplateColumns: `repeat(${Math.max(mobileSlotCount, 1)}, minmax(0, 1fr))` }}
        >
          {mobilePrimaryItems.map((item) => {
            const Icon = item.icon;
            const current = isCurrentPath(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-label={item.label}
                aria-current={current ? "page" : undefined}
                className={cn(
                  "flex min-h-14 min-w-0 flex-col items-center justify-center gap-1 rounded-[9px] px-1 text-[10px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring",
                  current ? "bg-border font-semibold" : "hover:bg-accent/70"
                )}
              >
                <Icon aria-hidden className="size-4" />
                <span className="max-w-full truncate">{item.mobileLabel ?? item.label}</span>
              </Link>
            );
          })}
          {mobileOverflowItems.length > 0 ? (
            (() => {
              const overflowCurrent = mobileOverflowItems.some((item) =>
                isCurrentPath(pathname, item.href)
              );
              // Overflow lands on the first hidden item's route; the full set
              // of remaining destinations is still one tap away from there
              // via the full sidebar / desktop nav, and each item keeps its
              // own <Link> so it's reachable directly by URL.
              const target = mobileOverflowItems[0];
              return (
                <Link
                  href={target.href}
                  aria-label={`More: ${mobileOverflowItems.map((item) => item.label).join(", ")}`}
                  aria-current={overflowCurrent ? "page" : undefined}
                  className={cn(
                    "flex min-h-14 min-w-0 flex-col items-center justify-center gap-1 rounded-[9px] px-1 text-[10px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring",
                    overflowCurrent ? "bg-border font-semibold" : "hover:bg-accent/70"
                  )}
                >
                  <MoreHorizontal aria-hidden className="size-4" />
                  <span className="max-w-full truncate">More</span>
                </Link>
              );
            })()
          ) : null}
        </nav>
      </div>
    </div>
  );
}
