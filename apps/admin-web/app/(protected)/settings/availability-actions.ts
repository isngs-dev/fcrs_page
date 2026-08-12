"use server";

/**
 * Tenant scheduling-availability save action (Tier 2, sales-call-booking
 * flow fix). Mirrors `settings/actions.ts#saveSettings`'s pattern as closely
 * as possible: a light Zod pre-check + `parseWeeklyHours` (the backend
 * remains authoritative -- `AvailabilityUpsertRequest`/`RulesPayload`,
 * `services/api/src/api/scheduling/admin_routes.py:60-73`, re-validates
 * everything, including the IANA timezone via `ZoneInfo`), then calls
 * `PUT /admin/schedule/availability` via `adminApiFetch`.
 *
 * CONFIRMED, not optimistic (same decision as `settings/actions.ts`): on a
 * `200`, the returned state carries the PUT response body's fresh
 * `timezone`/`rules`, never the raw submitted form values.
 * `revalidatePath("/settings")` re-syncs the RSC.
 */
import { revalidatePath } from "next/cache";
import { AdminApiError, adminApiFetch } from "@/lib/api";
import { availabilityFormSchema, parseWeeklyHours } from "@/lib/availability-schema";

export interface SaveAvailabilityFieldErrors {
  timezone?: string;
  slotMinutes?: string;
  bufferMinutes?: string;
  weeklyHoursJson?: string;
}

export interface SaveAvailabilityIdleState {
  status: "idle";
}

export interface SaveAvailabilityErrorState {
  status: "error";
  fieldErrors: SaveAvailabilityFieldErrors;
  formError: string | null;
  correlationId: string | null;
}

export interface AvailabilityResponseBody {
  timezone: string;
  rules: Record<string, unknown>;
  updated_at: string;
}

export interface SaveAvailabilitySuccessState {
  status: "saved";
  availability: AvailabilityResponseBody;
}

export type SaveAvailabilityState =
  | SaveAvailabilityIdleState
  | SaveAvailabilityErrorState
  | SaveAvailabilitySuccessState;

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

function errorState(partial: Omit<SaveAvailabilityErrorState, "status">): SaveAvailabilityErrorState {
  return { status: "error", ...partial };
}

export async function saveAvailability(
  _prevState: SaveAvailabilityState,
  formData: FormData
): Promise<SaveAvailabilityState> {
  const parsed = availabilityFormSchema.safeParse({
    timezone: String(formData.get("timezone") ?? ""),
    slotMinutes: String(formData.get("slotMinutes") ?? ""),
    bufferMinutes: String(formData.get("bufferMinutes") ?? ""),
    weeklyHoursJson: String(formData.get("weeklyHoursJson") ?? "{}"),
  });

  if (!parsed.success) {
    const fieldErrors: SaveAvailabilityFieldErrors = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path[0];
      if (key === "timezone") fieldErrors.timezone ??= issue.message;
      else if (key === "slotMinutes") fieldErrors.slotMinutes ??= issue.message;
      else if (key === "bufferMinutes") fieldErrors.bufferMinutes ??= issue.message;
    }
    return errorState({
      fieldErrors,
      formError: Object.keys(fieldErrors).length === 0 ? "Check the form and try again." : null,
      correlationId: null,
    });
  }

  const weeklyHoursResult = parseWeeklyHours(parsed.data.weeklyHoursJson);
  if (!weeklyHoursResult.ok) {
    return errorState({
      fieldErrors: { weeklyHoursJson: weeklyHoursResult.error },
      formError: null,
      correlationId: null,
    });
  }

  const requestBody = {
    timezone: parsed.data.timezone,
    rules: {
      slot_minutes: parsed.data.slotMinutes,
      buffer_minutes: parsed.data.bufferMinutes,
      weekly_hours: weeklyHoursResult.value,
    },
  };

  let response: Response;
  try {
    response = await adminApiFetch("/admin/schedule/availability", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapAdminApiError(err);
    }
    return errorState({ fieldErrors: {}, formError: GENERIC_NETWORK_ERROR, correlationId: null });
  }

  const body = (await response.json()) as AvailabilityResponseBody;

  revalidatePath("/settings");

  return { status: "saved", availability: body };
}

function mapAdminApiError(err: AdminApiError): SaveAvailabilityErrorState {
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return errorState({
      fieldErrors: {},
      formError: "You do not have permission to change scheduling availability.",
      correlationId: err.correlationId || null,
    });
  }
  if (err.status === 401) {
    return errorState({
      fieldErrors: {},
      formError: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    });
  }
  if (err.status === 422) {
    return errorState({
      fieldErrors: {
        timezone: /timezone/i.test(err.message) ? err.message : undefined,
      },
      formError: "The server rejected one or more values — check them and try again.",
      correlationId: err.correlationId || null,
    });
  }
  return errorState({
    fieldErrors: {},
    formError: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
    correlationId: err.correlationId || null,
  });
}
