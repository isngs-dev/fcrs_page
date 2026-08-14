"use server";

/**
 * "Connect Google Calendar" trigger (SR-22). Calls
 * `GET /admin/schedule/calendar/google/authorize` via `adminApiFetch`
 * (forwards the admin's own session cookie -- see `lib/api.ts`), then
 * `redirect()`s the admin's browser to the returned Google consent-screen
 * URL. Google eventually redirects back to
 * `GET /admin/schedule/calendar/google/callback` on the API, which itself
 * redirects into `/workspace?calendar_connected=true` or
 * `?calendar_error=<reason>` (`admin_routes.py`) -- `workspace/page.tsx`
 * reads those query params and passes them to `GoogleCalendarSection` as
 * `justConnected`/`callbackError`.
 *
 * No form fields here -- `useActionState` is used only for its built-in
 * pending state + a place to land an error (e.g.
 * `GOOGLE_OAUTH_NOT_CONFIGURED`) without leaving the page, mirroring
 * `availability-actions.ts`'s error-state shape.
 */
import { redirect } from "next/navigation";
import { AdminApiError, adminApiFetch } from "@/lib/api";

export interface ConnectGoogleCalendarIdleState {
  status: "idle";
}

export interface ConnectGoogleCalendarErrorState {
  status: "error";
  message: string;
  correlationId: string | null;
}

export type ConnectGoogleCalendarState =
  | ConnectGoogleCalendarIdleState
  | ConnectGoogleCalendarErrorState;

interface GoogleAuthorizeResponseBody {
  authorize_url: string;
}

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

export async function connectGoogleCalendar(
  _prevState: ConnectGoogleCalendarState,
  _formData: FormData
): Promise<ConnectGoogleCalendarState> {
  let response: Response;
  try {
    response = await adminApiFetch("/admin/schedule/calendar/google/authorize");
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapAdminApiError(err);
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, correlationId: null };
  }

  const body = (await response.json()) as GoogleAuthorizeResponseBody;

  // Outside the try/catch: redirect() throws a Next-internal control-flow
  // error that must propagate, never be caught as an app-level failure.
  redirect(body.authorize_url);
}

function mapAdminApiError(err: AdminApiError): ConnectGoogleCalendarErrorState {
  if (err.errorCode === "GOOGLE_OAUTH_NOT_CONFIGURED") {
    return {
      status: "error",
      message: "Google Calendar isn't configured on this deployment yet. Contact support to set it up.",
      correlationId: err.correlationId || null,
    };
  }
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return {
      status: "error",
      message: "You do not have permission to connect a calendar.",
      correlationId: err.correlationId || null,
    };
  }
  if (err.status === 401) {
    return {
      status: "error",
      message: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    };
  }
  return {
    status: "error",
    message: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
    correlationId: err.correlationId || null,
  };
}
