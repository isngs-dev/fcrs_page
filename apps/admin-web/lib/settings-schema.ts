/**
 * Shared (isomorphic) Zod schema + JSON helpers for the tenant bot-settings
 * edit form (S13.6 decision 8). Mirrors `AdminBotSettingsRequest`
 * (services/api/src/api/admin/settings_routes.py:27-33) field-for-field:
 * `greeting`/`escalation_policy` max 2000, `tone` max 100, `business_hours`
 * an arbitrary JSON object (no enforced schema -- decision 5). Used by both
 * the client form (friendly inline errors) and the server action (the
 * backend remains authoritative -- this is a courtesy pre-check, not a
 * substitute for the 422s the backend can still return).
 *
 * NOT `server-only` -- imported by both `settings-form.tsx` ("use client")
 * and `settings/actions.ts` ("use server"), same split as
 * `lib/tenant-schema.ts` (S13.2).
 */
import { z } from "zod";
import type { BotSettings } from "@/lib/settings";

export const settingsFormSchema = z.object({
  greeting: z
    .string()
    .trim()
    .max(2000, "Greeting must be 2000 characters or fewer.")
    .optional()
    // Empty string -> omitted, so the backend receives `null`, not `""`.
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  launcherLabel: z
    .string()
    .trim()
    .max(40, "Launcher label must be 40 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  sidebarWorkspaceLabel: z
    .string()
    .trim()
    .max(80, "Sidebar workspace label must be 80 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  dashboardTitle: z
    .string()
    .trim()
    .max(80, "Dashboard title must be 80 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  escalationPolicy: z
    .string()
    .trim()
    .max(2000, "Escalation policy must be 2000 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  tone: z
    .string()
    .trim()
    .max(100, "Tone must be 100 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  // Raw textarea contents -- parsed/validated separately by
  // `parseBusinessHours` (decision 5), not by this schema, since "valid JSON
  // object or blank" isn't expressible as a single Zod string rule with a
  // useful per-case error message.
  businessHoursText: z.string(),
  // Tier 2: the turn-count cap, editable from this same form. A blank field
  // -> `undefined` (the server field's "leave as-is" sentinel, mirrored by
  // `saveSettings` sending `turn_cap: null` -- `settings_routes.py` only
  // touches `tenant_orchestrator_configs` when it is explicitly provided).
  // Mirrors `AdminBotSettingsRequest.turn_cap`'s `ge=1` constraint
  // (`services/api/src/api/admin/settings_routes.py`).
  turnCap: z
    .string()
    .trim()
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined))
    .refine((value) => value === undefined || /^\d+$/.test(value), {
      message: "Turn cap must be a whole number.",
    })
    .transform((value) => (value === undefined ? undefined : Number(value)))
    .refine((value) => value === undefined || value >= 1, {
      message: "Turn cap must be at least 1.",
    }),
});

export type SettingsFormInput = z.input<typeof settingsFormSchema>;
export type SettingsFormParsed = z.output<typeof settingsFormSchema>;

export type ParseBusinessHoursResult =
  | { ok: true; value: Record<string, unknown> | null }
  | { ok: false; error: string };

const INVALID_BUSINESS_HOURS_MESSAGE =
  "Business hours must be a valid JSON object (or left blank).";

/**
 * Pure, unit-testable guard for the `business_hours` textarea (decision 5).
 * Blank/whitespace-only -> `{ok:true, value:null}` (the backend clears the
 * field). Valid JSON that parses to a plain object -> `{ok:true, value}`.
 * A JSON array/scalar, or malformed JSON, -> `{ok:false, error}` -- the
 * caller must NOT send the request in that case.
 */
export function parseBusinessHours(raw: string): ParseBusinessHoursResult {
  const trimmed = raw.trim();
  if (trimmed.length === 0) {
    return { ok: true, value: null };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false, error: INVALID_BUSINESS_HOURS_MESSAGE };
  }

  if (
    typeof parsed !== "object" ||
    parsed === null ||
    Array.isArray(parsed)
  ) {
    return { ok: false, error: INVALID_BUSINESS_HOURS_MESSAGE };
  }

  return { ok: true, value: parsed as Record<string, unknown> };
}

/**
 * Pretty-prints a `business_hours` value for the textarea's default content.
 * `null`/`undefined` -> `""` (an empty textarea, matching what a first-time
 * tenant sees and what `parseBusinessHours("")` round-trips to).
 */
export function stringifyBusinessHours(
  value: Record<string, unknown> | null | undefined
): string {
  if (value === null || value === undefined) return "";
  return JSON.stringify(value, null, 2);
}

/**
 * The four text-field values the settings form's inputs are controlled by.
 * A plain string mirror of `BotSettings`' qualitative fields, keyed to the
 * form's `name` attributes.
 */
export interface SettingsFieldValues {
  greeting: string;
  launcherLabel: string;
  sidebarWorkspaceLabel: string;
  dashboardTitle: string;
  businessHoursText: string;
  escalationPolicy: string;
  tone: string;
  turnCap: string;
}

/** Derives the field values a fresh/reset form should show for a given
 * server-authoritative `BotSettings` snapshot -- used both for the initial
 * mount and for the "confirmed, not optimistic" reset after a successful
 * save (decision 4). */
export function fieldValuesFromSettings(settings: BotSettings): SettingsFieldValues {
  return {
    greeting: settings.greeting ?? "",
    launcherLabel: settings.launcherLabel ?? "",
    sidebarWorkspaceLabel: settings.sidebarWorkspaceLabel ?? "",
    dashboardTitle: settings.dashboardTitle ?? "",
    businessHoursText: stringifyBusinessHours(settings.businessHours),
    escalationPolicy: settings.escalationPolicy ?? "",
    tone: settings.tone ?? "",
    turnCap: String(settings.turnCap),
  };
}

/**
 * Bug-fix (S13.6 follow-up): the settings form previously derived its
 * `defaultValue`s from `state.status === "saved" ? state.settings :
 * currentSettings` on every render. On an ERROR-state re-render (e.g. two
 * consecutive invalid `businessHoursText` submissions), that expression
 * falls through to the ORIGINAL server-loaded `currentSettings` -- not
 * whatever the user had just typed -- and because the underlying inputs are
 * Base UI `Field.Control`s reading an uncontrolled `defaultValue` prop, a
 * *changed* `defaultValue` on a re-render (without a remount) triggers Base
 * UI's "changing the default value state of an uncontrolled FieldControl
 * after being initialized" dev warning; meanwhile the DOM itself does NOT
 * revert (uncontrolled inputs ignore `defaultValue` after mount), so the
 * user's edit is silently still in the input -- but if the input value ever
 * *is* reset by something else (a remount, or a controlled implementation
 * done naively), the stale server value would be resubmitted, reproducing
 * "I fixed it and got the same error again".
 *
 * The fix: the form is now CONTROLLED, seeded once via
 * `fieldValuesFromSettings` on mount, and this pure function decides -- for
 * a given previous/next `SaveState` transition -- whether the controlled
 * values should be overwritten with the server's fresh values. It must be
 * `true` only on a genuine NEW "saved" transition (so a successful save
 * still shows the server's authoritative values -- "confirmed, not
 * optimistic", decision 4 is preserved) and `false` on every other
 * transition, in particular error <-> error (so in-progress edits made
 * between two failed submissions are never discarded).
 */
export function shouldResetFieldsToServerValues(
  prevState: { status: string },
  nextState: { status: string }
): boolean {
  return nextState.status === "saved" && prevState !== nextState;
}
