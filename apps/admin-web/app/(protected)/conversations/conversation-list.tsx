/**
 * 340px conversation list (HANDOFF-SPEC.md §3 "4a": "340px list (filter
 * pills, LIVE/LEAD/ENDED badges, active row = #ecece5 + 3px citron left
 * bar)"). Restyled from the mock's raw markup (`Chatbot System Designs.dc.
 * html#4a`, lines 29-58) to real data: initials avatar, name/visitor label,
 * approximate relative time, `summary` as the preview line, and the honest
 * ACTIVE/ENDED status badge (see `presentation.ts` for why there's no LIVE/
 * LEAD badge).
 *
 * Each row is a `<Link>` (not a div+onClick), matching `leads-table.tsx`'s
 * progressive-enhancement pattern -- keyboard/middle-click/right-click
 * navigable, works with JS disabled. Selected-conversation state lives in
 * the URL (`?conversation=<id>`), mirroring `?lead=<id>`.
 *
 * SR-27 slice 2: geometry fixed to `Console.dc.html:388-394` -- 30px avatar
 * (10px font), row padding 13px/16px with 11px gap, and a 20px-tall status
 * chip (10px font, 6x6px dot) replacing the old 9.5px text-only badge.
 */
import Link from "next/link";
import type { ConversationListItem } from "@/lib/conversations";
import { initialsFromVisitor, lastActivityAt, relativeTime, statusBadgeStyle, visitorLabel } from "@/app/(protected)/conversations/presentation";

function conversationHref(
  basePath: string,
  currentParams: URLSearchParams,
  conversationId: string
): string {
  const params = new URLSearchParams(currentParams);
  params.set("conversation", conversationId);
  return `${basePath}?${params.toString()}`;
}

export function ConversationList({
  items,
  basePath,
  currentParams,
  selectedConversationId,
}: {
  items: ConversationListItem[];
  basePath: string;
  currentParams: URLSearchParams;
  selectedConversationId?: string;
}) {
  if (items.length === 0) {
    return (
      <p role="status" className="p-5 text-[13px] text-muted-foreground">
        No conversations yet -- conversations started by your chatbot will appear here.
      </p>
    );
  }

  return (
    <ul className="flex flex-1 flex-col overflow-y-auto" aria-label="Conversations">
      {items.map((item) => {
        const selected = item.conversationId === selectedConversationId;
        const badge = statusBadgeStyle(item.status);
        const label = visitorLabel(item.visitorId);
        return (
          <li
            key={item.conversationId}
            className="border-t border-[var(--row-line)] first:border-t-0"
            // SR-15 D1: the selected row's citron left bar is deleted and
            // re-decided to the design's --primary ink -- selection is
            // already signaled by the background fill; the bar just needs
            // to read against it, not carry a second accent.
            style={
              selected
                ? { background: "#efeee6", borderLeft: "3px solid #333333" }
                : { borderLeft: "3px solid transparent" }
            }
          >
            <Link
              href={conversationHref(basePath, currentParams, item.conversationId)}
              scroll={false}
              aria-current={selected ? "true" : undefined}
              className="flex min-h-[44px] gap-[11px] px-4 py-[13px] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
            >
              <span className="grid size-[30px] shrink-0 place-items-center rounded-full bg-border text-[10px] font-bold text-[var(--ink-2)]">
                {initialsFromVisitor(item.visitorId)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center justify-between gap-2">
                  <span className="truncate text-[13px] font-semibold text-foreground">{label}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">
                    {relativeTime(lastActivityAt(item))}
                  </span>
                </span>
                <span className="mb-1.5 mt-0.5 block truncate text-[12px] text-muted-foreground">
                  {item.summary ?? `${item.messageCount} message${item.messageCount === 1 ? "" : "s"}`}
                </span>
                <span className="flex gap-1.5">
                  <span
                    className="flex h-5 items-center gap-1 rounded-[5px] px-2 text-[10px] font-bold"
                    style={{ background: badge.bg, color: badge.fg }}
                  >
                    {badge.dot ? (
                      <span
                        aria-hidden
                        className="size-[6px] rounded-full"
                        style={{ background: badge.dot }}
                      />
                    ) : null}
                    {badge.label}
                  </span>
                </span>
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
