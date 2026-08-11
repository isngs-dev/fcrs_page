/**
 * Notifications inbox (SR-21). Rebuilds the honest-empty-shell page SR-15
 * left in place (its own docstring said so): "a real feed needs its own
 * backend sprint: a notifications table + list endpoint + read/unread
 * state." That backend now exists (`notifications/events_repository.py`,
 * `GET /admin/notifications`, `PATCH .../read`, `PATCH .../read-all`), so
 * this page renders real items instead of the permanent empty state.
 *
 * `async` server component reading all state (`?category=`, `?page=`) from
 * `searchParams` and fetching once per navigation -- no client state beyond
 * the URL, mirroring `conversations/page.tsx`/`leads/page.tsx`'s
 * server-first pattern. Per-item "mark read" and "Mark all read" are thin
 * client components (`mark-read-button.tsx`) that call server actions and
 * `router.refresh()` -- no polling, no WebSocket/SSE (D7): the feed updates
 * on page load/refresh only, like every other surface in this console.
 *
 * D4 (LOCKED, deliberate deviation from the four-pill mockup,
 * `Console.dc.html:442-447`): exactly THREE live filter pills ship -- All /
 * Leads / System. There is NO Mentions pill and no `mentions` category
 * anywhere in this file or the API it calls (`lib/notifications.ts`'s
 * `NotificationCategory` type has only "leads"|"system") -- there is no
 * @-mention/comment/tagging concept anywhere in this product (SR-21 M6), so
 * a fourth pill would either be inert (the exact defect this sprint exists
 * to remove) or filter a permanently-empty set (which implies "you have no
 * mentions" rather than "mentions do not exist"). Both are the silent-no-op
 * CLAUDE.md §3 forbids. This is recorded here, not left as a silent
 * omission a future reader might mistake for a bug.
 *
 * The SR-15 scope docstring asserting "there is no feed endpoint" and its
 * `aria-disabled` inert-pill treatment are REMOVED by this sprint -- a
 * stale note asserting something now false must not survive.
 *
 * SR-27 slice 1: geometry-only restyle to `Console.dc.html:430-457` --
 * 28px/600 title, reference chip recipe (active near-black/#fbfaf7,
 * inactive white + 1px border, replacing the old bg-primary/border-border
 * pills), 22px/24px card padding, 9px pill gap, a 64px real SVG-bell empty
 * state (dropping the emoji), 17px/600 empty title, 380px max-width empty
 * body, and the hardcoded `#fdfdec` row-highlight fallback replaced with the
 * design token. **D11 re-confirmed a third time: still exactly THREE
 * pills** -- see the D4 note above, unchanged by this sprint.
 */
import { requireAnyRole } from "@/lib/auth";
import {
  listNotifications,
  notificationTargetHref,
  type NotificationCategory,
  type NotificationEvent,
} from "@/lib/notifications";
import { SoftCard } from "@/components/admin/soft-card";
import Link from "next/link";
import { MarkAllReadButton, MarkReadButton } from "@/app/(protected)/notifications/mark-read-button";

interface NotificationsPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

// D4: exactly three live pills. No "mentions" entry -- see module docstring.
const FILTER_PILLS: { label: string; value: NotificationCategory | undefined }[] = [
  { label: "All", value: undefined },
  { label: "Leads", value: "leads" },
  { label: "System", value: "system" },
];

const KIND_ICONS: Record<string, string> = {
  lead_captured: "🧲",
  lead_assigned: "🙋",
  lead_auto_assigned: "🔁",
  lead_stage_transitioned: "📶",
  lead_converted: "✅",
  conversation_escalated: "🚨",
  ingestion_failed: "⚠️",
};

const KIND_LABELS: Record<string, string> = {
  lead_captured: "New lead captured",
  lead_assigned: "Lead assigned",
  lead_auto_assigned: "Lead auto-assigned",
  lead_stage_transitioned: "Lead stage changed",
  lead_converted: "Lead converted",
  conversation_escalated: "Conversation escalated",
  ingestion_failed: "Knowledge ingestion failed",
};

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diffMs = now - then;
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}d ago`;
}

function pillHref(category: NotificationCategory | undefined): string {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  const qs = params.toString();
  return qs ? `/notifications?${qs}` : "/notifications";
}

function NotificationRow({
  item,
  revalidatePathTarget,
}: {
  item: NotificationEvent;
  revalidatePathTarget: string;
}) {
  const href = notificationTargetHref(item);
  const icon = KIND_ICONS[item.kind] ?? "🔔";
  const label = KIND_LABELS[item.kind] ?? item.kind;

  const content = (
    <div className="flex flex-1 items-center gap-3 min-w-0">
      <div
        aria-hidden="true"
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-secondary text-[16px]"
      >
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13.5px] font-semibold text-foreground">{label}</p>
        <p className="text-[11.5px] text-muted-foreground">{relativeTime(item.createdAt)}</p>
      </div>
    </div>
  );

  return (
    <li
      className={
        "flex items-center gap-3 border-b border-[var(--row-line)] px-6 py-3 last:border-b-0 " +
        (item.read ? "" : "bg-secondary border-l-[3px] border-l-primary")
      }
    >
      {href ? (
        <Link href={href} className="flex flex-1 items-center gap-3 min-w-0 no-underline">
          {content}
        </Link>
      ) : (
        content
      )}
      <MarkReadButton eventId={item.eventId} read={item.read} revalidatePathTarget={revalidatePathTarget} />
    </li>
  );
}

export default async function NotificationsPage({ searchParams }: NotificationsPageProps) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT", "PLATFORM_ADMIN");

  const params = await searchParams;
  const rawCategory = firstValue(params.category);
  const category: NotificationCategory | undefined =
    rawCategory === "leads" || rawCategory === "system" ? rawCategory : undefined;

  const revalidatePathTarget = pillHref(category);
  const result = await listNotifications({ page: 1, category });

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h1 className="text-[28px] font-semibold text-foreground">Notifications</h1>
        <p className="text-[13px] text-muted-foreground">
          Lead activity and system events for your workspace.
        </p>
      </div>

      <SoftCard className="overflow-hidden">
        <div className="flex items-center justify-between gap-3 border-b border-border px-6 py-[22px]">
          <div className="flex items-center gap-2">
            <span className="text-[17px] font-semibold text-foreground">Inbox</span>
            {result.status === "ok" && result.unreadCount > 0 && (
              <span className="rounded-full bg-primary px-2 py-0.5 text-[11px] font-semibold text-primary-foreground">
                {result.unreadCount} new
              </span>
            )}
          </div>
          {result.status === "ok" && result.items.length > 0 && (
            <MarkAllReadButton category={category} revalidatePathTarget={revalidatePathTarget} />
          )}
        </div>

        {/* Filter pills -- D4/D11: exactly three live values, URL-driven.
            Reference chip recipe: active near-black/#fbfaf7, inactive white
            + 1px border (not the old bg-primary/muted-foreground pattern). */}
        <div
          className="flex flex-wrap gap-[9px] border-b border-[var(--row-line)] px-6 py-3"
          role="group"
          aria-label="Filter notifications by category"
        >
          {FILTER_PILLS.map((pill) => {
            const active = pill.value === category;
            return (
              <Link
                key={pill.label}
                href={pillHref(pill.value)}
                aria-current={active ? "true" : undefined}
                className="rounded-full px-3 py-[5px] text-[11.5px] font-semibold transition-colors"
                style={
                  active
                    ? { background: "#333333", color: "#fbfaf7" }
                    : { background: "#fff", border: "1px solid var(--line)", color: "var(--ink-2)" }
                }
              >
                {pill.label}
              </Link>
            );
          })}
        </div>

        {result.status === "error" && (
          <div role="alert" className="px-6 py-10 text-center">
            <p className="text-[14px] font-semibold text-foreground">{result.message}</p>
            {result.correlationId && (
              <p className="mt-1 text-[11.5px] text-muted-foreground">
                Correlation ID: {result.correlationId}
              </p>
            )}
          </div>
        )}

        {result.status === "ok" && result.items.length === 0 && (
          <div
            role="status"
            className="flex min-h-[360px] flex-col items-center justify-center gap-3 px-6 py-16 text-center"
          >
            {/* P5: 64px cream circle + real SVG bell (stroke #878787,
                26x26), replacing the emoji -- Console.dc.html:449 verbatim
                path data/stroke-width. */}
            <div
              aria-hidden="true"
              className="mb-2 flex h-16 w-16 items-center justify-center rounded-full bg-secondary"
            >
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#878787" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
                <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
              </svg>
            </div>
            <p className="text-[17px] font-semibold text-foreground">No notifications yet</p>
            <p className="max-w-[380px] text-[13px] leading-[1.5] text-muted-foreground">
              Lead activity and system events will show up here as they happen.
            </p>
          </div>
        )}

        {result.status === "ok" && result.items.length > 0 && (
          <ul className="flex flex-col">
            {result.items.map((item) => (
              <NotificationRow key={item.eventId} item={item} revalidatePathTarget={revalidatePathTarget} />
            ))}
          </ul>
        )}
      </SoftCard>
    </div>
  );
}
