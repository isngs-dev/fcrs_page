"use client";

import { useState, useSyncExternalStore, type PointerEvent as ReactPointerEvent } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BarChart3,
  Bell,
  BookOpen,
  Building2,
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

interface NavItem {
  href: string;
  label: string;
  mobileLabel?: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  roles: Role[];
}

/**
 * Nav groups mirror HANDOFF-SPEC.md's Sidebar recipe (Overview / Tools / Team).
 * PLATFORM_ADMIN sees a distinct "Platform" group instead, since Clients is a
 * different persona's workspace, not a peer of the client-facing tools.
 */
interface NavGroup {
  label: string;
  items: NavItem[];
}

const overviewItems: NavItem[] = [
  {
    href: "/",
    label: "Dashboard",
    icon: LayoutDashboard,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT"],
  },
  { href: "/leads", label: "Leads", icon: UsersRound, roles: ["CLIENT_ADMIN", "CLIENT_AGENT"] },
  {
    href: "/conversations",
    label: "Conversations",
    icon: MessageSquare,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT"],
  },
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
    href: "/analytics",
    label: "Analytics",
    icon: BarChart3,
    roles: ["CLIENT_ADMIN", "CLIENT_AGENT"],
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

const navGroups: NavGroup[] = [
  { label: "Overview", items: overviewItems },
  { label: "Tools", items: toolsItems },
  { label: "Team", items: teamItems },
  { label: "Platform", items: platformItems },
];

function isCurrentPath(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
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
  const visibleGroups = navGroups
    .map((group) => ({
      label: group.label,
      items: group.items.filter((item) => item.roles.includes(role)),
    }))
    .filter((group) => group.items.length > 0);
  const visibleItems = visibleGroups.flatMap((group) => group.items);
  const isPlatformAdmin = role === "PLATFORM_ADMIN";
  const roleLabel = identityRoleLabel ?? (isPlatformAdmin ? "Platform admin" : "Client workspace");
  const workspaceLabel =
    sidebarWorkspaceLabel?.trim() || (isPlatformAdmin ? "Platform workspace" : "Client workspace");

  // Cap the mobile bottom nav at MOBILE_NAV_MAX slots: if there's room for
  // every item, show them all; otherwise reserve the last slot for a "More"
  // overflow link to the first tool page rather than letting a 6th+ item
  // disappear or the row overflow off-screen.
  const mobilePrimaryItems =
    visibleItems.length <= MOBILE_NAV_MAX ? visibleItems : visibleItems.slice(0, MOBILE_NAV_MAX - 1);
  const mobileOverflowItems =
    visibleItems.length <= MOBILE_NAV_MAX ? [] : visibleItems.slice(MOBILE_NAV_MAX - 1);
  const mobileSlotCount = mobilePrimaryItems.length + (mobileOverflowItems.length > 0 ? 1 : 0);

  return (
    <div className="flex min-h-screen bg-[#fbfbf8] text-[#191a17]">
      <aside
        style={{ width: sidebar.collapsed ? SIDEBAR_COLLAPSED_WIDTH : sidebar.width }}
        className={cn(
          "sticky top-0 relative hidden h-screen shrink-0 flex-col border-r border-[#e7e7e2] bg-[#f7f7f3] py-4 lg:flex",
          sidebar.collapsed ? "px-2" : "px-3.5"
        )}
      >
        <div className={cn("flex items-center gap-2.5 px-2 pb-6", sidebar.collapsed && "flex-col")}>
          <div className="grid size-[34px] place-items-center rounded-[10px] bg-[#191a17] text-xs font-bold text-[#e4f222]">
            CL
          </div>
          <div className={cn("min-w-0 flex-1", sidebar.collapsed && "hidden")}>
            <p className="text-sm font-bold tracking-[-0.02em]">ChatLeads</p>
            <p className="truncate text-[11px] text-[#96978e]">
              {workspaceLabel}
            </p>
          </div>
          <button
            type="button"
            aria-label={sidebar.collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!sidebar.collapsed}
            onClick={() => updateSidebar((current) => ({ ...current, collapsed: !current.collapsed }))}
            className="grid size-8 shrink-0 place-items-center rounded-lg text-[#5a5b54] transition-colors hover:bg-[#ecece5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
          >
            {sidebar.collapsed ? <PanelLeftOpen aria-hidden className="size-4" /> : <PanelLeftClose aria-hidden className="size-4" />}
          </button>
        </div>

        <nav aria-label="Main navigation" className="flex flex-col gap-4 overflow-y-auto">
          {visibleGroups.map((group) => (
            <div key={group.label} className="flex flex-col gap-1">
              <p className={cn("px-2 py-1 text-[10.5px] font-semibold tracking-[0.06em] text-[#a8a99f] uppercase", sidebar.collapsed && "hidden")}>
                {group.label}
              </p>
              {group.items.map((item) => {
                const Icon = item.icon;
                const current = isCurrentPath(pathname, item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    aria-label={item.label}
                    aria-current={current ? "page" : undefined}
                    className={cn(
                      "flex min-h-11 items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]",
                      sidebar.collapsed && "justify-center px-0",
                      current
                        ? "bg-[#ecece5] font-semibold text-[#191a17]"
                        : "text-[#45463f] hover:bg-[#ecece5]/70"
                    )}
                    >
                      <Icon aria-hidden className="size-4" />
                    <span className={sidebar.collapsed ? "hidden" : undefined}>{item.label}</span>
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="mt-auto border-t border-[#e7e7e2] pt-3">
          <div className={cn("flex items-center gap-2.5 rounded-lg px-2 py-2", sidebar.collapsed && "justify-center px-0")}>
            <div className="grid size-8 shrink-0 place-items-center rounded-full bg-[#dcdcd2] text-[10px] font-bold text-[#5a5b54]">
              {identityLabel.slice(0, 2).toUpperCase()}
            </div>
            <div className={cn("min-w-0 flex-1", sidebar.collapsed && "hidden")}>
              <p className="min-w-0 truncate text-xs font-semibold">{identityLabel}</p>
              <p className="min-w-0 truncate text-[11px] text-[#96978e]">{roleLabel}</p>
            </div>
          </div>
          <form action={logoutAction}>
            <button
              type="submit"
              aria-label="Log out"
              className="flex min-h-11 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13px] text-[#5a5b54] transition-colors hover:bg-[#ecece5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
            >
              <LogOut aria-hidden className="size-4" />
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
        <header className="flex min-h-16 items-center border-b border-[#e7e7e2] bg-[#fbfbf8] px-4 lg:hidden">
          <Link href="/" className="flex items-center gap-2 text-sm font-bold">
            <span className="grid size-8 place-items-center rounded-lg bg-[#191a17] text-[10px] text-[#e4f222]">
              CL
            </span>
            ChatLeads
          </Link>
          <form action={logoutAction} className="ml-auto">
            <button
              type="submit"
              aria-label="Log out"
              className="grid size-11 place-items-center rounded-lg text-[#45463f] transition-colors hover:bg-[#ecece5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
            >
              <LogOut aria-hidden className="size-4" />
            </button>
          </form>
        </header>
        <div className="flex min-w-0 flex-1 flex-col pb-20 lg:pb-0">{children}</div>
        <nav
          aria-label="Mobile navigation"
          className="fixed inset-x-0 bottom-0 z-40 grid min-h-16 border-t border-[#e7e7e2] bg-[#fbfbf8]/95 px-2 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden"
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
                  "flex min-h-14 min-w-0 flex-col items-center justify-center gap-1 rounded-lg px-1 text-[10px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#191a17]",
                  current ? "bg-[#ecece5] font-semibold" : "hover:bg-[#ecece5]/70"
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
                    "flex min-h-14 min-w-0 flex-col items-center justify-center gap-1 rounded-lg px-1 text-[10px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#191a17]",
                    overflowCurrent ? "bg-[#ecece5] font-semibold" : "hover:bg-[#ecece5]/70"
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
