"use server";

/**
 * "Connect Calendly" server action -- stores a tenant's Calendly hosted-
 * handoff config via the EXISTING `PUT /admin/schedule/calendar` endpoint
 * (`admin_routes.py`). This is the counterpart to `calendar-actions.ts`'s
 * Google OAuth flow, but Calendly has no OAuth dance on this app's side --
 * the admin creates the webhook subscription on Calendly's own side (their
 * API, outside this app) using a signing secret they choose, then pastes
 * that same secret + their scheduling URL here so this backend can verify
 * Calendly's webhook signature (`api.scheduling.calendly_webhook`).
 *
 * `enabled` is the field with real behavioral weight (see
 * `google-calendar-section.tsx`'s sibling doc comment style): `true` means
 * the widget skips native slot-picking entirely and always hands the
 * visitor off to this Calendly link; `false` means the link is only
 * embedded as a reschedule option in confirmation emails while native
 * booking (e.g. Google Calendar) keeps handling actual bookings. Both this
 * form and the backend genuinely support either value -- it's not a
 * mistake to leave it off.
 *
 * No `GET /admin/schedule/calendar` endpoint exists (confirmed against
 * `admin_routes.py` before writing this) -- same documented limitation as
 * `availability-actions.ts`: this form cannot be pre-populated with the
 * tenant's current config, only show the just-confirmed values after a
 * successful save.
 */
import { revalidatePath } from "next/cache";
import { z } from "zod";
import { AdminApiError, adminApiFetch } from "@/lib/api";

const calendlyFormSchema = z.object({
  schedulingUrl: z
    .string()
    .trim()
    .min(1, "Your Calendly scheduling link is required.")
    .url("Enter a valid URL, e.g. https://calendly.com/your-name/30min."),
  signingSecret: z
    .string()
    .trim()
    .min(1, "The webhook signing secret is required."),
  enabled: z.boolean(),
});

export interface SaveCalendlyFieldErrors {
  schedulingUrl?: string;
  signingSecret?: string;
}

export interface SaveCalendlyIdleState {
  status: "idle";
}

export interface SaveCalendlyErrorState {
  status: "error";
  fieldErrors: SaveCalendlyFieldErrors;
  formError: string | null;
}

export interface SaveCalendlySuccessState {
  status: "saved";
  schedulingUrl: string;
  enabled: boolean;
}

export type SaveCalendlyState =
  | SaveCalendlyIdleState
  | SaveCalendlyErrorState
  | SaveCalendlySuccessState;

interface CalendarConfigResponseBody {
  provider: string;
  calendar_id: string | null;
  enabled: boolean;
  scheduling_url: string | null;
}

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

function errorState(partial: Omit<SaveCalendlyErrorState, "status">): SaveCalendlyErrorState {
  return { status: "error", ...partial };
}

export async function saveCalendlyConfig(
  _prevState: SaveCalendlyState,
  formData: FormData
): Promise<SaveCalendlyState> {
  const parsed = calendlyFormSchema.safeParse({
    schedulingUrl: formData.get("schedulingUrl"),
    signingSecret: formData.get("signingSecret"),
    enabled: formData.get("enabled") === "on",
  });

  if (!parsed.success) {
    const fieldErrors: SaveCalendlyFieldErrors = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path[0];
      if (key === "schedulingUrl") fieldErrors.schedulingUrl ??= issue.message;
      else if (key === "signingSecret") fieldErrors.signingSecret ??= issue.message;
    }
    return errorState({
      fieldErrors,
      formError: Object.keys(fieldErrors).length === 0 ? "Check the form and try again." : null,
    });
  }

  const { schedulingUrl, signingSecret, enabled } = parsed.data;

  const requestBody = {
    provider: "calendly",
    calendar_id: null,
    credentials: signingSecret,
    enabled,
    busy: [],
    scheduling_url: schedulingUrl,
  };

  let response: Response;
  try {
    response = await adminApiFetch("/admin/schedule/calendar", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapAdminApiError(err);
    }
    return errorState({ fieldErrors: {}, formError: GENERIC_NETWORK_ERROR });
  }

  const body = (await response.json()) as CalendarConfigResponseBody;

  revalidatePath("/workspace");

  return {
    status: "saved",
    schedulingUrl: body.scheduling_url ?? schedulingUrl,
    enabled: body.enabled,
  };
}

function mapAdminApiError(err: AdminApiError): SaveCalendlyErrorState {
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return errorState({
      fieldErrors: {},
      formError: "You do not have permission to change these settings.",
    });
  }
  if (err.status === 401) {
    return errorState({
      fieldErrors: {},
      formError: "Your session has expired. Please sign in again.",
    });
  }
  if (err.status === 422) {
    return errorState({
      fieldErrors: {},
      formError: "The server rejected one or more values — check them and try again.",
    });
  }
  return errorState({
    fieldErrors: {},
    formError: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
  });
}
