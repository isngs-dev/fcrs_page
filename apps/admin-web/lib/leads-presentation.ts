/**
 * Client-safe presentation layer for leads (4b). Pure types + pure lookup
 * functions only -- no `server-only` import, no data fetching -- so Client
 * Components (`lead-drawer.tsx`) can import them directly without pulling
 * the server-only `lib/leads.ts` (and its `adminApiFetch`/`server-only`
 * import) into the client bundle.
 *
 * `lib/leads.ts` re-exports everything from this module so existing server
 * consumers (`leads-table.tsx`, `lib/dashboard.ts`, tests importing from
 * `@/lib/leads`) keep working unchanged -- this file is the single source of
 * truth for these types/functions, `lib/leads.ts` just forwards them.
 */

/** A single row of `GET /admin/leads` -- mirrors `LeadListItem`
 * (admin_routes.py:136-140) exactly. No `tenant_id`/`visitor_id`/consent --
 * the backend response is already leak-free by construction.
 *
 * Lives HERE rather than in `lib/leads.ts` (which re-exports it) for the same
 * reason every other type in this file does: `leads-board.tsx` is a Client
 * Component and must not have any import edge -- not even a type-only one --
 * to the `server-only` `lib/leads.ts`. */
export interface LeadListItem {
  leadId: string;
  name: string | null;
  email: string | null;
  phone: string | null;
  status: string;
  stage: string;
  qualificationScore: number | null;
  assignedAgentId: string | null;
  source: string;
  createdAt: string;
}

/** Same leak-free shape as `LeadListItem` minus `createdAt` (mirrors
 * `LeadDetailResponse`, admin_routes.py:106-117). */
export interface LeadDetail {
  leadId: string;
  name: string;
  email: string;
  phone: string | null;
  status: string;
  stage: string;
  qualificationScore: number | null;
  assignedAgentId: string | null;
  source: string;
}

export type LeadDetailResult =
  | { status: "ok"; lead: LeadDetail }
  | { status: "error"; message: string; correlationId: string };

/** A single timeline entry -- mirrors `LeadActivityResponse`
 * (admin_routes.py:84-92) exactly, no `tenant_id`. `type` is one of
 * `stage_change` | `note` | `assignment` (the three activity types the
 * backend ever writes, per `admin_routes.py`'s `add_activity` call sites). */
export interface LeadActivityItem {
  activityId: string;
  leadId: string;
  type: string;
  payload: Record<string, unknown> | null;
  actor: string | null;
  createdAt: string;
}

export type LeadActivitiesResult =
  | { status: "ok"; items: LeadActivityItem[] }
  | { status: "error"; message: string; correlationId: string };

// ---------------------------------------------------------------------------
// 4b design tokens -- pure lookup helpers (HANDOFF-SPEC.md §2 Badges).
// Kept as pure functions (unit-testable, no JSX) per this repo's convention
// of testing pure logic rather than rendering.
// ---------------------------------------------------------------------------

export interface BadgeStyle {
  label: string;
  bg: string;
  fg: string;
}

// SR-15 D1: "converted"'s citron foreground is deleted and re-decided to the
// design's --success-fg green (#3f7d57) on the design's --success-bg
// (#eaf3ec) -- converted is this table's one meaningfully "good" stage, and
// M3's success chip is the system's only sanctioned use of color as
// semantic signal (D4: this label always carries its own text, never color
// alone).
const STAGE_BADGES: Record<string, BadgeStyle> = {
  captured: { label: "CAPTURED", bg: "#eeebe3", fg: "#3d4540" },
  qualified: { label: "QUALIFIED", bg: "#e4ece3", fg: "#1f4b3a" },
  contacted: { label: "CONTACTED", bg: "#f6e7dc", fg: "#9a4a1e" },
  converted: { label: "CONVERTED", bg: "#e5efe7", fg: "#2e6a46" },
  disqualified: { label: "DISQUALIFIED", bg: "#f8e6e0", fg: "#b3452f" },
};

/** Stage -> badge color/label (HANDOFF-SPEC.md §2 Badges). Unknown stages
 * fall back to a neutral muted style rather than throwing -- the backend's
 * `_VALID_STAGES` set is the real gate, this is presentation only. */
export function stageBadgeStyle(stage: string): BadgeStyle {
  return STAGE_BADGES[stage] ?? { label: stage.toUpperCase(), bg: "#eeebe3", fg: "#3d4540" };
}

/** Score chip color. `null` (no score yet) renders as the muted em-dash,
 * handled by the caller.
 *
 * SR-24 item 15 (real bug fix, not a restyle): the >=60 branch previously
 * used a legacy citron hex (`#eef7a8`) left over from the pre-SR-15 Ink &
 * Citron palette. That hex has no token backing it and doesn't match this
 * console's monochrome-plus-one-success-pair system (globals.css's
 * `--success-bg`/`--success-fg`). Replaced with the design system's success
 * tokens so a high score reads as the same "good" signal the converted-stage
 * chip already uses, instead of a stray one-off color. */
export function scoreChipStyle(score: number, stage: string): BadgeStyle {
  if (stage === "converted") {
    return { label: String(score), bg: "#e5efe7", fg: "#2e6a46" };
  }
  if (score >= 60) {
    return { label: String(score), bg: "var(--success-bg)", fg: "var(--success-fg)" };
  }
  return { label: String(score), bg: "transparent", fg: "var(--muted-foreground)" };
}

/** Two-letter initials for the assigned-agent avatar chip, e.g. "Sara R."
 * -> "SR". Falls back to "?" for an empty/whitespace-only name so the avatar
 * never renders blank. */
export function initialsFromName(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}
