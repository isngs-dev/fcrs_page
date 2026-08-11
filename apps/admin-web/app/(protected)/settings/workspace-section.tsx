"use client";

/**
 * Workspace settings section (SR-20 D5, amended; geometry rebuilt SR-27
 * slice 7). Live fields: name / URL (slug) / timezone, rendered through the
 * shared `SetRow` primitive (`Console.dc.html:880-883`) as the "General"
 * `.tbl-card`. Language renders as a DISABLED `SetRow` (S3 in the SR-27
 * handoff, D3) -- same row geometry as a live field, but visibly
 * non-interactive with the SR-20 D5 note preserved verbatim. Billing and
 * Delete workspace are NOT rendered here at all -- Billing is a rail entry
 * only (`settings-rail.tsx`), Delete workspace is `disabled-sections.tsx`'s
 * own Danger-zone card.
 */
import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SoftCard } from "@/components/admin/soft-card";
import { SetRow, SET_ROW_FIELD_CLASS } from "@/app/(protected)/settings/set-row";
import { saveWorkspace, type SaveWorkspaceState } from "@/app/(protected)/settings/workspace-actions";
import type { Workspace } from "@/lib/workspace";

const initialState: SaveWorkspaceState = { status: "idle" };

function SaveButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" disabled={pending} className="sr-only focus:not-sr-only">
      {/* The visible Save action lives in the shell's sticky header
          (page.tsx); this hidden submit keeps the form keyboard/Enter-
          submittable and gives assistive tech a real labeled control. */}
      {pending ? "Saving…" : "Save workspace"}
    </Button>
  );
}

export function WorkspaceSection({ currentWorkspace }: { currentWorkspace: Workspace }) {
  const [state, formAction] = useActionState(saveWorkspace, initialState);

  const [name, setName] = useState(currentWorkspace.name);
  const [slug, setSlug] = useState(currentWorkspace.slug);
  const [timezone, setTimezone] = useState(currentWorkspace.timezone);

  const fieldErrors = state.status === "error" ? state.fieldErrors : {};
  const formError = state.status === "error" ? state.formError : null;

  return (
    <SoftCard className="flex scroll-mt-16 flex-col px-[22px] pb-2 pt-1" id="settings-workspace">
      <h2 className="pb-0.5 pt-4 text-[15px] font-semibold text-foreground">General</h2>

      {state.status === "saved" ? (
        <p role="status" className="pt-2 text-xs text-muted-foreground">
          Saved.
        </p>
      ) : null}
      {formError ? (
        <p role="alert" className="mt-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <form action={formAction} id="workspace-form">
        <SetRow label="Workspace name" description="Shown in the sidebar and on invitations." htmlFor="workspace-name">
          <Input
            id="workspace-name"
            name="name"
            value={name}
            onValueChange={setName}
            maxLength={200}
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.name ? (
            <p role="alert" className="mt-1 text-sm text-destructive">
              {fieldErrors.name}
            </p>
          ) : null}
        </SetRow>

        <SetRow label="Workspace URL" description="Your unique console address." htmlFor="workspace-slug">
          <Input
            id="workspace-slug"
            name="slug"
            value={slug}
            onValueChange={setSlug}
            maxLength={63}
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.slug ? (
            <p role="alert" className="mt-1 text-sm text-destructive">
              {fieldErrors.slug}
            </p>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              Lowercase letters, numbers and hyphens only. Must be unique across the platform.
            </p>
          )}
        </SetRow>

        <SetRow label="Time zone" description="Used for reports and business hours." htmlFor="workspace-timezone">
          <Input
            id="workspace-timezone"
            name="timezone"
            value={timezone}
            onValueChange={setTimezone}
            placeholder="Europe/London"
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.timezone ? (
            <p role="alert" className="mt-1 text-sm text-destructive">
              {fieldErrors.timezone}
            </p>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              An IANA time zone identifier, e.g. &quot;Europe/London&quot; — not a fixed
              offset. Used for display only; reports are not re-bucketed by this yet.
            </p>
          )}
        </SetRow>

        {/* D3: Language stays a DISABLED set-row, never a live field -- no
            `language` anywhere on `AdminWorkspaceRequest`/`Response`
            (G18, `admin/workspace_routes.py:17-19`). */}
        <SetRow label="Language" description="Console display language." isLast>
          <div
            aria-disabled="true"
            className="flex w-full items-center justify-between rounded-[10px] border border-[var(--line)] bg-secondary px-3 py-2.5 text-[13px] text-muted-foreground"
          >
            <span>Not configured</span>
            <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium">Coming soon</span>
          </div>
        </SetRow>

        <div className="py-3">
          <SaveButton />
        </div>
      </form>
    </SoftCard>
  );
}
