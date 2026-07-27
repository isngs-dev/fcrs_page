"use client";

/**
 * Tenant bot-settings edit form (S13.6 decisions 1, 4-7). A thin client
 * component pre-populated from the server-loaded `currentSettings`, wired to
 * the `"use server"` `saveSettings` action via `useActionState`. Renders a
 * read-only view when `canEdit` is false (defensive -- the page only mounts
 * this for CLIENT_ADMIN; a CLIENT_AGENT gets `ReadOnlySettings` directly, see
 * page.tsx).
 *
 * Bug fix (post-S13.6): the fields are CONTROLLED, not uncontrolled
 * `defaultValue`s. Previously each `Textarea`/`Input` used
 * `defaultValue={displaySettings.field}` where `displaySettings` fell back to
 * `currentSettings` (the ORIGINAL server-loaded value) on every non-"saved"
 * render -- including error <-> error re-renders. Because the inputs are
 * Base UI `Field.Control`s (`components/ui/input.tsx` wraps
 * `@base-ui/react/input`), which read `defaultValue` once and warn in dev if
 * it changes on a later render, this produced the "changing the default
 * value state of an uncontrolled FieldControl" console warning -- the
 * visible symptom of the same root cause described below. See
 * `lib/settings-schema.ts`'s `shouldResetFieldsToServerValues` doc comment
 * for the full analysis. Controlled state here is seeded once on mount from
 * `currentSettings`, and is only overwritten with the server's fresh values
 * on a genuine NEW "saved" transition (preserving decision 4: confirmed, not
 * optimistic) -- never on an error re-render, so in-progress edits made
 * between two failed submissions survive.
 *
 * Restyle (6a, UI-only): sections regrouped into Persona / Behavior / Install
 * per HANDOFF-SPEC.md §3's 6a line, with a 420px live preview pane
 * (`widget-preview.tsx`) and sticky Publish/Discard actions. The
 * controlled-`fields` state, the `saveSettings` wiring, and
 * `shouldResetFieldsToServerValues` gating are UNCHANGED -- only markup/
 * layout moved. Only REAL backend fields are editable here (greeting,
 * businessHoursText, escalationPolicy, tone); the mock's "Bot name",
 * "Suggested questions", behavior toggles, fallback/qualification dropdowns,
 * and appearance swatches have no backend field to bind to (see
 * `lib/settings.ts` / `lib/settings-schema.ts`) and are rendered as an
 * explicit "not available yet" gap notice rather than fake, no-op controls.
 */
import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { saveSettings, type SaveState } from "@/app/(protected)/settings/actions";
import {
  fieldValuesFromSettings,
  shouldResetFieldsToServerValues,
} from "@/lib/settings-schema";
import type { BotSettings } from "@/lib/settings";
import { InstallSnippet } from "@/app/(protected)/settings/install-snippet";
import { WidgetPreview } from "@/app/(protected)/settings/widget-preview";

const initialState: SaveState = { status: "idle" };

function PublishButton({ dirty }: { dirty: boolean }) {
  const { pending } = useFormStatus();
  return (
    <Button
      type="submit"
      disabled={pending || !dirty}
      className="bg-[#191a17] text-[#e4f222] hover:bg-[#191a17]/90 disabled:opacity-40"
    >
      {pending ? "Publishing…" : "Publish changes"}
    </Button>
  );
}

export function SettingsForm({
  currentSettings,
  tenantId,
}: {
  currentSettings: BotSettings;
  tenantId?: string;
}) {
  const [state, formAction] = useActionState(saveSettings.bind(null, tenantId), initialState);

  // Controlled field state, seeded once from the server-loaded snapshot.
  // `useState`'s lazy initializer only runs on mount, so this does NOT
  // re-derive on every render -- that's the whole point (see file-header
  // comment and `shouldResetFieldsToServerValues`'s doc comment).
  const [fields, setFields] = useState(() => fieldValuesFromSettings(currentSettings));

  // Discard confirmation (guardrails skill / ui-ux-pro-max §1: confirm before
  // discarding unsaved changes). `serverFields` is the last server-confirmed
  // snapshot -- it only ever moves forward on a genuine NEW "saved"
  // transition (same gating as `fields` above), so "dirty" compares against
  // what's actually persisted, never the original page-load values after a
  // save, and never an in-flight/failed submission.
  const [serverFields, setServerFields] = useState(() => fieldValuesFromSettings(currentSettings));
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);

  // Track the previous `state` reference so we can detect a genuine NEW
  // "saved" transition during render (React's documented "adjusting state
  // during render" pattern -- avoids an extra effect-driven render/flash and
  // avoids re-running on unrelated parent re-renders, since `state` is only
  // a new object when `useActionState` produces a new result).
  const [prevState, setPrevState] = useState(state);
  if (shouldResetFieldsToServerValues(prevState, state)) {
    // `state.status === "saved"` is guaranteed by
    // `shouldResetFieldsToServerValues`, but narrow explicitly for TS.
    if (state.status === "saved") {
      const savedFields = fieldValuesFromSettings(state.settings);
      setFields(savedFields);
      setServerFields(savedFields);
    }
    setPrevState(state);
  } else if (prevState !== state) {
    setPrevState(state);
  }

  const fieldErrors = state.status === "error" ? state.fieldErrors : {};
  const formError = state.status === "error" ? state.formError : null;

  const isDirty =
    fields.greeting !== serverFields.greeting ||
    fields.launcherLabel !== serverFields.launcherLabel ||
    fields.sidebarWorkspaceLabel !== serverFields.sidebarWorkspaceLabel ||
    fields.dashboardTitle !== serverFields.dashboardTitle ||
    fields.businessHoursText !== serverFields.businessHoursText ||
    fields.escalationPolicy !== serverFields.escalationPolicy ||
    fields.tone !== serverFields.tone;

  function handleDiscard() {
    setFields(serverFields);
    setConfirmingDiscard(false);
  }

  return (
    <form action={formAction} className="flex flex-col gap-5">
      {state.status === "saved" ? (
        <p role="status" className="rounded-md border border-input bg-muted/50 p-3 text-sm">
          Saved.
        </p>
      ) : null}
      {formError ? (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
        {/* Left: form sections */}
        <div className="flex min-w-0 flex-1 flex-col gap-5">
          {/* Persona */}
          <section className="flex flex-col gap-3.5 rounded-[14px] border border-[#e7e7e2] p-5">
            <h2 className="text-sm font-bold text-[#191a17]">Persona</h2>

            <div className="flex flex-col gap-2">
              <Label htmlFor="greeting">Greeting message</Label>
              <Textarea
                id="greeting"
                name="greeting"
                value={fields.greeting}
                onChange={(e) => setFields((f) => ({ ...f, greeting: e.target.value }))}
                maxLength={2000}
                rows={3}
                placeholder="Hi! How can I help you today?"
              />
              {fieldErrors.greeting ? (
                <p role="alert" className="text-sm text-destructive">
                  {fieldErrors.greeting}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">Up to 2000 characters.</p>
              )}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="tone">Tone</Label>
              <Input
                id="tone"
                name="tone"
                value={fields.tone}
                onValueChange={(value) => setFields((f) => ({ ...f, tone: value }))}
                maxLength={100}
                placeholder="friendly, professional, concise"
              />
              {fieldErrors.tone ? (
                <p role="alert" className="text-sm text-destructive">
                  {fieldErrors.tone}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Free text (up to 100 characters) — e.g. &quot;friendly&quot;,
                  &quot;professional&quot;, &quot;concise&quot;. Not restricted to a fixed list.
                </p>
              )}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="launcherLabel">Launcher label</Label>
              <Input
                id="launcherLabel"
                name="launcherLabel"
                value={fields.launcherLabel}
                onValueChange={(value) => setFields((f) => ({ ...f, launcherLabel: value }))}
                maxLength={40}
                placeholder="Chat with us"
              />
              {fieldErrors.launcherLabel ? (
                <p role="alert" className="text-sm text-destructive">
                  {fieldErrors.launcherLabel}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Up to 40 characters. Leave blank to use “Chat with us”.
                </p>
              )}
            </div>

            <div className="flex flex-col gap-1.5 rounded-md border border-dashed border-[#d5d5cb] bg-[#f7f7f3] p-3">
              <p className="text-xs font-semibold text-[#5a5b54]">
                Bot name &amp; suggested questions — coming soon
              </p>
              <p className="text-xs text-[#70716a]">
                These aren&apos;t configurable yet; there&apos;s no backend field for them.
              </p>
            </div>
          </section>

          {/* Workspace */}
          <section className="flex flex-col gap-3.5 rounded-[14px] border border-[#e7e7e2] p-5">
            <h2 className="text-sm font-bold text-[#191a17]">Workspace</h2>
            <p className="text-xs text-[#70716a]">
              These labels personalize this tenant&apos;s sidebar and dashboard only.
            </p>

            <div className="flex flex-col gap-2">
              <Label htmlFor="sidebarWorkspaceLabel">Sidebar workspace label</Label>
              <Input
                id="sidebarWorkspaceLabel"
                name="sidebarWorkspaceLabel"
                value={fields.sidebarWorkspaceLabel}
                onValueChange={(value) =>
                  setFields((f) => ({ ...f, sidebarWorkspaceLabel: value }))
                }
                maxLength={80}
                placeholder="Client workspace"
              />
              {fieldErrors.sidebarWorkspaceLabel ? (
                <p role="alert" className="text-sm text-destructive">
                  {fieldErrors.sidebarWorkspaceLabel}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Up to 80 characters. Leave blank for “Client workspace”.
                </p>
              )}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="dashboardTitle">Dashboard title</Label>
              <Input
                id="dashboardTitle"
                name="dashboardTitle"
                value={fields.dashboardTitle}
                onValueChange={(value) => setFields((f) => ({ ...f, dashboardTitle: value }))}
                maxLength={80}
                placeholder="Dashboard"
              />
              {fieldErrors.dashboardTitle ? (
                <p role="alert" className="text-sm text-destructive">
                  {fieldErrors.dashboardTitle}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Up to 80 characters. Leave blank for “Dashboard”.
                </p>
              )}
            </div>
          </section>

          {/* Behavior */}
          <section className="flex flex-col gap-3.5 rounded-[14px] border border-[#e7e7e2] p-5">
            <h2 className="text-sm font-bold text-[#191a17]">Behavior</h2>

            <div className="flex flex-col gap-2">
              <Label htmlFor="escalationPolicy">Escalation policy</Label>
              <Textarea
                id="escalationPolicy"
                name="escalationPolicy"
                value={fields.escalationPolicy}
                onChange={(e) =>
                  setFields((f) => ({ ...f, escalationPolicy: e.target.value }))
                }
                maxLength={2000}
                rows={3}
                placeholder="Escalate to a human agent when the visitor asks for a refund."
              />
              {fieldErrors.escalationPolicy ? (
                <p role="alert" className="text-sm text-destructive">
                  {fieldErrors.escalationPolicy}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">Up to 2000 characters.</p>
              )}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="businessHoursText">Business hours (JSON)</Label>
              <Textarea
                id="businessHoursText"
                name="businessHoursText"
                value={fields.businessHoursText}
                onChange={(e) =>
                  setFields((f) => ({ ...f, businessHoursText: e.target.value }))
                }
                rows={6}
                className="font-mono text-sm"
                placeholder={'{\n  "mon": ["09:00", "17:00"]\n}'}
              />
              {fieldErrors.businessHoursText ? (
                <p role="alert" className="text-sm text-destructive">
                  {fieldErrors.businessHoursText}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  A JSON object, e.g. {"{"}&quot;mon&quot;: [&quot;09:00&quot;, &quot;17:00&quot;]
                  {"}"} — or leave blank.
                </p>
              )}
            </div>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-md border border-input bg-muted/50 p-3.5 text-sm sm:grid-cols-3">
              <div>
                <dt className="text-xs text-muted-foreground">Answer threshold</dt>
                <dd>{currentSettings.answerThreshold}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Escalate threshold</dt>
                <dd>{currentSettings.escalateThreshold}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Turn cap</dt>
                <dd>{currentSettings.turnCap}</dd>
              </div>
              <div className="col-span-2 sm:col-span-3">
                <p className="text-xs text-muted-foreground">
                  Fallback behavior and qualification threshold are governed by these read-only
                  values — not editable from this screen.
                </p>
              </div>
            </dl>
          </section>

          {/* Install */}
          <InstallSnippet />

          <div className="flex flex-col gap-1.5 rounded-md border border-dashed border-[#d5d5cb] bg-[#f7f7f3] p-3.5">
            <p className="text-xs font-semibold text-[#5a5b54]">
              Widget appearance — coming soon
            </p>
            <p className="text-xs text-[#70716a]">
              Accent color and launcher position swatches aren&apos;t wired to a backend field
              yet, so they aren&apos;t shown as editable controls here.
            </p>
          </div>
        </div>

        {/* Right: live preview, 420px per HANDOFF-SPEC.md §3 */}
        <aside className="flex w-full shrink-0 flex-col items-center gap-3.5 rounded-[14px] border border-[#e7e7e2] bg-[#f7f7f3] p-6 lg:w-[420px]">
          <WidgetPreview
            greeting={fields.greeting}
            tone={fields.tone}
            launcherLabel={fields.launcherLabel}
          />
        </aside>
      </div>

      {/* Sticky Publish (ink/citron) + Discard, per HANDOFF-SPEC.md §3 */}
      <div className="sticky bottom-0 -mx-1 flex items-center gap-2.5 border-t border-[#e7e7e2] bg-[#fbfbf8]/95 px-1 py-3 backdrop-blur">
        {confirmingDiscard ? (
          <>
            <p className="text-xs text-muted-foreground">Discard unsaved changes?</p>
            <Button type="button" variant="outline" size="sm" onClick={handleDiscard}>
              Discard
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setConfirmingDiscard(false)}
            >
              Keep editing
            </Button>
          </>
        ) : (
          <Button
            type="button"
            variant="outline"
            disabled={!isDirty}
            onClick={() => setConfirmingDiscard(true)}
            className="disabled:opacity-40"
          >
            Discard
          </Button>
        )}
        <div className="ml-auto">
          <PublishButton dirty={isDirty} />
        </div>
      </div>
    </form>
  );
}
