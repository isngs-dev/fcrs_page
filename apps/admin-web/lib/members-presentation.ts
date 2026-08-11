/**
 * Client-safe presentation layer for the Team members screen (7b). Pure
 * types + pure lookup functions only -- no `server-only` import -- so the
 * client components under `app/(protected)/members/**` can import them
 * directly. Mirrors the split established by `lib/leads-presentation.ts`.
 *
 * Role badges use the REAL 2-role model returned by the backend
 * (`services/common/src/common/auth.py`'s `Role` enum: `CLIENT_ADMIN` /
 * `CLIENT_AGENT` -- `GET /admin/users` only ever lists these two for a
 * tenant, per `users_routes.py`'s `require_roles(Role.CLIENT_ADMIN)` gate
 * and `create_tenant_agent` hardcoding new users to `CLIENT_AGENT`). There
 * is no third "VIEWER" tier server-side, so none is invented here -- the
 * mock's ADMIN/AGENT color treatments (HANDOFF-SPEC.md §3 "7b") are reused
 * verbatim for the two roles that actually exist.
 */

export interface BadgeStyle {
  label: string;
  bg: string;
  fg: string;
}

// SR-15 D1: the ADMIN badge's citron foreground is deleted and re-decided
// to white -- it already reads as the emphasized role on the dark badge
// fill without a chromatic accent, matching the shell's identical
// re-decision.
const ROLE_BADGES: Record<string, BadgeStyle> = {
  CLIENT_ADMIN: { label: "ADMIN", bg: "#333333", fg: "#ffffff" },
  CLIENT_AGENT: { label: "AGENT", bg: "#efeee6", fg: "#404040" },
};

/** Role -> badge color/label (SR-15 restyle of HANDOFF-SPEC.md §3 "7b":
 * "ADMIN badge ink/white, AGENT/VIEWER #ecece5" -- VIEWER omitted, it does
 * not exist in the real `Role` enum). Unknown roles (e.g. a `PLATFORM_ADMIN`
 * somehow showing up here) fall back to a neutral muted style rather than
 * throwing. */
export function roleBadgeStyle(role: string): BadgeStyle {
  return ROLE_BADGES[role] ?? { label: role, bg: "#ecece5", fg: "var(--ink-2)" };
}

/** Two-letter initials for the member avatar chip, e.g. "Sara Romero" ->
 * "SR". Falls back to the first two letters of the email local-part, then
 * "?", so the avatar never renders blank. */
export function initialsFromMember(name: string | null, email: string): string {
  const source = name && name.trim().length > 0 ? name : email.split("@")[0] ?? "";
  const parts = source.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/** Relative "last active" label from an ISO timestamp, or the honest "Never
 * logged in" when `lastLoginAt` is `null` (never fabricate a fake "Now"). */
export function formatLastActive(lastLoginAt: string | null): string {
  if (!lastLoginAt) return "Never logged in";
  const then = new Date(lastLoginAt).getTime();
  if (Number.isNaN(then)) return "Never logged in";
  const diffMs = Date.now() - then;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return new Date(lastLoginAt).toLocaleDateString();
}
