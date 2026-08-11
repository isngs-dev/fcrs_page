/**
 * Client-safe presentation helpers for the Conversations console (design id
 * 4a). Pure types + pure lookup functions only -- no `server-only` import --
 * so the client interaction components (`conversation-list.tsx`,
 * `takeover-composer.tsx`) can import them without pulling `lib/
 * conversations.ts`'s `server-only`/`adminApiFetch` import into the client
 * bundle. Mirrors `@/lib/leads-presentation.ts`'s split.
 *
 * Honest-mapping notes (mandated scope decisions -- see conversations
 * page.tsx header for the full rationale):
 *  - The real `status` field is only `active`/`ended`. There is no "LIVE"
 *    (real-time) signal and no "LEAD" linkage field on the backend response.
 *    `statusBadgeStyle` maps `active` -> "ACTIVE" (not "LIVE" -- never claim
 *    real-time push the backend doesn't provide) and `ended` -> "ENDED".
 *    There is no "LEAD" badge anywhere in this module.
 *  - `relativeTime` is an approximate, clearly-labelled heuristic (e.g. "2m
 *    ago") derived from a single ISO timestamp -- not a live/push signal.
 */

export interface BadgeStyle {
  label: string;
  bg: string;
  fg: string;
  /** 6x6px status dot color (Console.dc.html:394); `undefined` renders no dot. */
  dot?: string;
}

/** Status -> badge color/label. Deliberately only covers the two real
 * backend values (`active`/`ended`, admin_routes.py `_VALID_STATUSES`) --
 * an unrecognized value falls back to a neutral muted style rather than
 * throwing or inventing a "LIVE"-style badge.
 *
 * SR-27 slice 2: `bg`/`fg` fixed to the reference success pair exactly
 * (`#eaf3ec` on `#3f7d57`) with a `#4a9c6d` 6x6px dot (`Console.dc.html:394`). */
export function statusBadgeStyle(status: string): BadgeStyle {
  if (status === "active") {
    return { label: "ACTIVE", bg: "#eaf3ec", fg: "#3f7d57", dot: "#4a9c6d" };
  }
  if (status === "ended") {
    return { label: "ENDED", bg: "#efeee6", fg: "var(--ink-2)" };
  }
  return { label: status.toUpperCase(), bg: "#efeee6", fg: "var(--ink-2)" };
}

/** Two-letter initials for the visitor avatar chip. A `null` id (fully
 * anonymous visitor) falls back to "?" rather than throwing. */
export function initialsFromVisitor(visitorId: string | null): string {
  if (!visitorId) return "?";
  const trimmed = visitorId.trim();
  if (trimmed.length === 0) return "?";
  return trimmed.slice(0, 2).toUpperCase();
}

/** Display label for a conversation row -- `visitorId` when present,
 * otherwise a generic "Anonymous visitor" (never fabricate a name; the
 * backend has no visitor-name field at all). */
export function visitorLabel(visitorId: string | null): string {
  return visitorId ? `Visitor ${visitorId}` : "Anonymous visitor";
}

/**
 * Approximate, explicitly-labelled relative time from an ISO timestamp,
 * e.g. "2m ago", "3h ago", "5d ago". Not a live/push signal -- it is
 * recomputed only on each server render/page load, per the scope decision
 * that this is a static/polling read-only UI. Returns "just now" for <60s
 * and falls back to the raw ISO string if the timestamp doesn't parse.
 */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return iso;

  const diffMs = now.getTime() - then.getTime();
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.floor(diffHour / 24);
  return `${diffDay}d ago`;
}

/** The most recent activity timestamp for a conversation row -- `endedAt`
 * if the conversation has ended, otherwise `startedAt` (there is no
 * per-message "last activity" field on `ConversationListItem`, only on the
 * detail's messages, so the list's recency label is necessarily coarse --
 * hence `relativeTime`'s "approximate" framing). */
export function lastActivityAt(item: { startedAt: string; endedAt: string | null }): string {
  return item.endedAt ?? item.startedAt;
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export { formatDateTime };
