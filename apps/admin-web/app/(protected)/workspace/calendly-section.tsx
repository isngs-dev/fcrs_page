"use client";

/**
 * "Connect Calendly" settings section. Stores the tenant's Calendly hosted-
 * handoff config (`calendly-actions.ts`) -- pairs with the Calendly-side
 * webhook subscription the admin creates on Calendly's own dashboard/API
 * (outside this app; see the setup instructions linked in the description
 * below). Mirrors `availability-section.tsx`'s "own SoftCard, own
 * useActionState, own PUT endpoint, starts blank (no GET endpoint exists)"
 * pattern.
 */
import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { SoftCard } from "@/components/admin/soft-card";
import { SetRow, SET_ROW_FIELD_CLASS } from "@/components/admin/set-row";
import {
  saveCalendlyConfig,
  type SaveCalendlyState,
} from "@/app/(protected)/workspace/calendly-actions";

const initialState: SaveCalendlyState = { status: "idle" };

function SaveButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" disabled={pending}>
      {pending ? "Saving…" : "Save Calendly settings"}
    </Button>
  );
}

export function CalendlySection() {
  const [state, formAction] = useActionState(saveCalendlyConfig, initialState);

  const [schedulingUrl, setSchedulingUrl] = useState("");
  const [signingSecret, setSigningSecret] = useState("");
  const [enabled, setEnabled] = useState(false);

  const fieldErrors = state.status === "error" ? state.fieldErrors : {};
  const formError = state.status === "error" ? state.formError : null;

  return (
    <SoftCard className="flex scroll-mt-16 flex-col px-[22px] pb-4 pt-1" id="settings-calendly">
      <h2 className="pb-0.5 pt-4 text-[15px] font-semibold text-foreground">Calendly</h2>
      <p className="pb-2 text-xs text-muted-foreground">
        Requires a webhook subscription created on Calendly&apos;s own side first (their API,
        not this form) using the same signing secret you enter below.
      </p>

      {state.status === "saved" ? (
        <p role="status" className="mb-2 rounded-md border border-border bg-secondary p-3 text-sm text-foreground">
          Saved. Calendly is {state.enabled ? "handling bookings directly (native picker skipped)" : "linked as a reschedule option only"}.
        </p>
      ) : null}
      {formError ? (
        <p role="alert" className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <form action={formAction}>
        <SetRow
          label="Scheduling link"
          description="Your Calendly booking page, e.g. https://calendly.com/your-name/30min."
          htmlFor="calendly-scheduling-url"
        >
          <input
            id="calendly-scheduling-url"
            name="schedulingUrl"
            value={schedulingUrl}
            onChange={(e) => setSchedulingUrl(e.target.value)}
            placeholder="https://calendly.com/your-name/30min"
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.schedulingUrl ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.schedulingUrl}
            </p>
          ) : null}
        </SetRow>

        <SetRow
          label="Webhook signing secret"
          description="The same secret value you used when creating the webhook subscription on Calendly's side."
          htmlFor="calendly-signing-secret"
        >
          <input
            id="calendly-signing-secret"
            name="signingSecret"
            type="password"
            value={signingSecret}
            onChange={(e) => setSigningSecret(e.target.value)}
            placeholder="••••••••••••••••"
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.signingSecret ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.signingSecret}
            </p>
          ) : null}
        </SetRow>

        <SetRow
          label="Replace native booking with Calendly"
          description={
            'When ON, visitors are always sent to your Calendly page instead of picking a time in the widget directly -- Calendly becomes the ONLY way to book. When OFF, your existing native booking flow (e.g. Google Calendar) keeps handling bookings, and this link is only added as a reschedule option in confirmation emails.'
          }
          htmlFor="calendly-enabled"
          isLast
        >
          <label className="flex items-center gap-2 text-sm text-foreground" htmlFor="calendly-enabled">
            <input
              id="calendly-enabled"
              name="enabled"
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="size-4"
            />
            Replace native booking entirely
          </label>
        </SetRow>

        <div className="py-3">
          <SaveButton />
        </div>
      </form>
    </SoftCard>
  );
}
