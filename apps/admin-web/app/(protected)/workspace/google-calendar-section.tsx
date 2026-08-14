"use client";

/**
 * "Connect Google Calendar" section (SR-22). A single button that submits
 * `connectGoogleCalendar` (`calendar-actions.ts`) -- on success the action
 * itself redirects the admin's browser to Google's consent screen, so there
 * is no "saved" state to render here, only the in-flight/error ones.
 *
 * `justConnected`/`callbackError` are read by `workspace/page.tsx` from the
 * `?calendar_connected=true` / `?calendar_error=<reason>` query params that
 * `GET /admin/schedule/calendar/google/callback` redirects back with
 * (`admin_routes.py`) -- this is a full page round-trip through Google, so
 * that state can only arrive via the URL, never via this component's own
 * `useActionState`. `CALLBACK_ERROR_MESSAGES` maps each of the callback
 * route's five deterministic `calendar_error` reason codes to admin-facing
 * text; an unrecognized code (a future backend addition this component
 * hasn't been updated for) still shows a message rather than the raw code.
 */
import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { SoftCard } from "@/components/admin/soft-card";
import {
  connectGoogleCalendar,
  type ConnectGoogleCalendarState,
} from "@/app/(protected)/workspace/calendar-actions";

const initialState: ConnectGoogleCalendarState = { status: "idle" };

const CALLBACK_ERROR_MESSAGES: Record<string, string> = {
  access_denied: "Google Calendar access was denied. You can try connecting again.",
  missing_code_or_state: "The connection attempt was incomplete. Please try again.",
  invalid_state: "That connection link expired or was already used. Please try again.",
  not_configured: "Google Calendar isn't configured on this deployment yet. Contact support to set it up.",
  exchange_failed: "Google couldn't complete the connection. Please try again.",
};

function ConnectButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" disabled={pending}>
      {pending ? "Connecting…" : "Connect Google Calendar"}
    </Button>
  );
}

export function GoogleCalendarSection({
  justConnected,
  callbackError,
}: {
  justConnected: boolean;
  callbackError: string | null;
}) {
  const [state, formAction] = useActionState(connectGoogleCalendar, initialState);

  const actionError = state.status === "error" ? state.message : null;
  const redirectError = callbackError
    ? CALLBACK_ERROR_MESSAGES[callbackError] ?? "Something went wrong connecting Google Calendar."
    : null;

  return (
    <SoftCard className="flex scroll-mt-16 flex-col px-[22px] pb-4 pt-1" id="settings-google-calendar">
      <h2 className="pb-0.5 pt-4 text-[15px] font-semibold text-foreground">
        Google Calendar
      </h2>
      <p className="pb-2 text-xs text-muted-foreground">
        Connect a Google Calendar so booked calls create a real event with a Google Meet link,
        sent to the visitor in their confirmation email.
      </p>

      {justConnected ? (
        <p role="status" className="mb-2 rounded-md border border-border bg-secondary p-3 text-sm text-foreground">
          Google Calendar connected.
        </p>
      ) : null}
      {redirectError ? (
        <p role="alert" className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {redirectError}
        </p>
      ) : null}
      {actionError ? (
        <p role="alert" className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {actionError}
        </p>
      ) : null}

      <form action={formAction}>
        <ConnectButton />
      </form>
    </SoftCard>
  );
}
