/**
 * Shared (isomorphic) Zod schema + JSON helpers for the tenant scheduling-
 * availability settings form (Tier 2, sales-call-booking flow fix). Mirrors
 * `AvailabilityUpsertRequest`/`RulesPayload`
 * (services/api/src/api/scheduling/admin_routes.py:33-73) field-for-field:
 * `timezone` an IANA zone (authoritatively validated server-side via
 * `ZoneInfo`, this is a courtesy pre-check), `slot_minutes` > 0,
 * `buffer_minutes` >= 0, `weekly_hours` a `{day: [[start, end], ...]}` map
 * keyed by `mon`..`sun` with `HH:MM` 24h times and `start < end`.
 *
 * NOT `server-only` -- imported by both the client form component
 * ("use client") and the server action ("use server"), same split as
 * `lib/settings-schema.ts`.
 */
import { z } from "zod";

export const DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type DayKey = (typeof DAY_KEYS)[number];

export const DAY_LABELS: Record<DayKey, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

/** One day's hours as edited in the UI -- a single [start, end] window
 * (`RulesPayload.weekly_hours` allows multiple windows per day, but this UI
 * -- like `business-hours-picker.tsx` -- only expresses one, the common
 * case). `null` means the day has no hours (closed). */
export type DayWindow = [string, string] | null;
export type WeeklyHoursState = Record<DayKey, DayWindow>;

/** The seeded default this platform ships with (Tier 1 data-seeding step:
 * Mon-Fri, 09:00-18:00 UTC) -- used both as the form's initial state (there
 * is no `GET /admin/schedule/availability` endpoint to read the current
 * value back from, see the module doc in `availability-section.tsx`) and as
 * a sane "Reset to default" action. */
export const DEFAULT_WEEKLY_HOURS: WeeklyHoursState = {
  mon: ["09:00", "18:00"],
  tue: ["09:00", "18:00"],
  wed: ["09:00", "18:00"],
  thu: ["09:00", "18:00"],
  fri: ["09:00", "18:00"],
  sat: null,
  sun: null,
};

const HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

export const availabilityFormSchema = z.object({
  timezone: z.string().trim().min(1, "Time zone is required."),
  slotMinutes: z
    .string()
    .trim()
    .refine((v) => /^\d+$/.test(v) && Number(v) > 0, {
      message: "Slot length must be a whole number of minutes greater than 0.",
    })
    .transform((v) => Number(v)),
  bufferMinutes: z
    .string()
    .trim()
    .refine((v) => /^\d+$/.test(v), {
      message: "Buffer must be a whole number of minutes (0 or more).",
    })
    .transform((v) => Number(v)),
  // The day-toggle grid's serialized state -- parsed/validated separately
  // by `parseWeeklyHours` (decision mirrors `settings-schema.ts`'s
  // `parseBusinessHours` split), since the day/window shape isn't
  // expressible as a single Zod string rule with a useful per-case message.
  weeklyHoursJson: z.string(),
});

export type AvailabilityFormParsed = z.output<typeof availabilityFormSchema>;

export type ParseWeeklyHoursResult =
  | { ok: true; value: Record<string, string[][]> }
  | { ok: false; error: string };

const INVALID_WEEKLY_HOURS_MESSAGE =
  "Enter valid 24h HH:MM times with the open time before the close time.";

/**
 * Validates the day-toggle grid's serialized `WeeklyHoursState` JSON into
 * the exact `{day: [[start, end]]}` shape `RulesPayload.weekly_hours`
 * expects. At least one day must be open -- an all-closed week is rejected
 * client-side (the backend would accept it, but a bookable-call flow with
 * zero open days is never useful, so this UI does not offer it).
 */
export function parseWeeklyHours(raw: string): ParseWeeklyHoursResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, error: INVALID_WEEKLY_HOURS_MESSAGE };
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: INVALID_WEEKLY_HOURS_MESSAGE };
  }

  const result: Record<string, string[][]> = {};
  for (const [day, value] of Object.entries(parsed as Record<string, unknown>)) {
    if (value === null) continue;
    if (!(DAY_KEYS as readonly string[]).includes(day)) {
      return { ok: false, error: INVALID_WEEKLY_HOURS_MESSAGE };
    }
    if (
      !Array.isArray(value) ||
      value.length !== 2 ||
      typeof value[0] !== "string" ||
      typeof value[1] !== "string"
    ) {
      return { ok: false, error: INVALID_WEEKLY_HOURS_MESSAGE };
    }
    const [start, end] = value;
    if (!HHMM_RE.test(start) || !HHMM_RE.test(end) || start >= end) {
      return { ok: false, error: INVALID_WEEKLY_HOURS_MESSAGE };
    }
    result[day] = [[start, end]];
  }

  if (Object.keys(result).length === 0) {
    return { ok: false, error: "At least one day must be open." };
  }

  return { ok: true, value: result };
}

/** Serializes the day-toggle grid's state to the JSON string carried by the
 * form's hidden `weeklyHoursJson` field. */
export function serializeWeeklyHours(state: WeeklyHoursState): string {
  return JSON.stringify(state);
}
