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
 * SR-15 D9 restructure (this sprint): a sticky page header with Discard/
 * Publish, a 184px left section-nav (Persona / Behavior / Install --
 * Workspace and Appearance are SR-20's, per scope item 12's exclusion), a
 * scrolling two-column body of `TableCard`-styled sections, and a 300px
 * `position:sticky` right column holding the existing `widget-preview.tsx`.
 * This is a RE-LAYOUT, not a re-spec (D9): the controlled-`fields` state,
 * the `saveSettings` wiring, `shouldResetFieldsToServerValues` gating, and
 * every field's validation are UNCHANGED -- only markup/layout moved, and
 * `actions.ts` is untouched. Only REAL backend fields are editable here
 * (greeting, launcherLabel, sidebarWorkspaceLabel, dashboardTitle,
 * businessHoursText, escalationPolicy, tone); the mock's "Bot name",
 * "Suggested questions", behavior toggles, fallback/qualification dropdowns,
 * and appearance swatches have no backend field to bind to (see
 * `lib/settings.ts` / `lib/settings-schema.ts`) and are rendered as an
 * explicit "not available yet" gap notice rather than fake, no-op controls.
 * `sidebarWorkspaceLabel`/`dashboardTitle` are folded into Persona (they are
 * real, already-shipped fields) rather than given their own "Workspace"
 * section-nav entry, since D9 reserves that label for SR-20's real
 * workspace-settings screen.
 *
 * SR-27 slice 8: markup rebuilt to `Console.dc.html:500-557` geometry --
 * five-entry icon rail (Persona/Behavior/Workspace/Install/Appearance,
 * Appearance disabled -- no appearance field exists, matching the Billing
 * treatment in the Settings shell), Persona/Behavior sections re-rendered
 * through the shared `SetRow` primitive, a real business-hours day-picker
 * popover (`business-hours-picker.tsx`) alongside the existing raw-JSON
 * textarea (both write the same `businessHoursText` state), thresholds
 * rendered as 3 read-only chips. The escalation-policy field's real type
 * was verified per the handoff's evidence-gathering instruction:
 * `lib/settings.ts:22` -- `escalationPolicy: string | null` -- so it stays
 * a text/textarea field; NO numeric minus/plus stepper is built for it, and
 * no `number-stepper.tsx` file exists in this change. ALL state/wiring
 * (controlled `fields`, `saveSettings` action, `shouldResetFieldsToServerValues`
 * gating, every validation) is UNCHANGED -- this is markup-only. The header
 * Publish button keeps its existing verb ("Publish changes") since that
 * already accurately describes the wired `PUT /admin/settings` action.
 */
import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { saveSettings, type SaveState } from "@/app/(protected)/settings/actions";
import {
  fieldValuesFromSettings,
  shouldResetFieldsToServerValues,
} from "@/lib/settings-schema";
import type { BotSettings } from "@/lib/settings";
import { InstallSnippet } from "@/app/(protected)/settings/install-snippet";
import { WidgetPreview } from "@/app/(protected)/settings/widget-preview";
import { SetRow, SET_ROW_FIELD_CLASS } from "@/components/admin/set-row";
import { SettingsRail, type SettingsRailRow } from "@/components/admin/settings-rail";
import { BusinessHoursPicker } from "@/app/(protected)/settings/business-hours-picker";

const initialState: SaveState = { status: "idle" };

const PERSONA_ICON = (
  <svg aria-hidden width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
    <circle cx="12" cy="8" r="4" />
    <path d="M4 20c0-4.4 3.6-8 8-8s8 3.6 8 8" />
  </svg>
);
const BEHAVIOR_ICON = (
  <svg aria-hidden width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
    <path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8" />
  </svg>
);
const INSTALL_ICON = (
  <svg aria-hidden width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
    <path d="M8 6 3 12l5 6M16 6l5 6-5 6" />
  </svg>
);
const APPEARANCE_ICON = (
  <svg aria-hidden width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor" stroke="none" />
  </svg>
);

function PublishButton({ dirty }: { dirty: boolean }) {
  const { pending } = useFormStatus();
  return (
    <Button
      type="submit"
      disabled={pending || !dirty}
      className="bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
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
    fields.tone !== serverFields.tone ||
    fields.turnCap !== serverFields.turnCap ||
    fields.lowConfidenceStreakCap !== serverFields.lowConfidenceStreakCap;

  function handleDiscard() {
    setFields(serverFields);
    setConfirmingDiscard(false);
  }

  return (
    <form action={formAction} className="flex flex-col gap-0">
      {/* SR-15 D9: sticky page header with Discard/Publish -- replaces the
          old bottom-sticky action bar. */}
      <div className="sticky top-0 z-10 -mx-1 flex flex-wrap items-center gap-2.5 border-b border-border bg-background/95 px-1 py-3 backdrop-blur">
        <div>
          <h2 className="text-sm font-bold text-foreground">Bot settings</h2>
          {state.status === "saved" ? (
            <p role="status" className="text-xs text-muted-foreground">
              Saved.
            </p>
          ) : null}
        </div>
        <div className="ml-auto flex items-center gap-2.5">
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
          <PublishButton dirty={isDirty} />
        </div>
      </div>

      {formError ? (
        <p role="alert" className="mt-4 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <div className="flex flex-col gap-6 pt-5 lg:flex-row lg:items-start">
        {/* SR-27 slice 8, later trimmed to four entries (Workspace row
            removed on user request -- it was a dead in-page anchor left
            over from before the `/settings` and `/workspace` route split;
            see workspace/page.tsx's doc comment for that split): icon rail
            via the shared SettingsRail primitive (Persona/Behavior/Install/
            Appearance). Persona/Behavior/Install are in-page anchors (this
            shell owns those sections); Appearance is disabled -- no
            appearance/theme field exists anywhere on the bot-settings or
            workspace payloads (matching the Billing treatment in the
            Settings shell). */}
        <SettingsRail
          eyebrow="Sections"
          rows={
            [
              { kind: "anchor", key: "persona", label: "Persona", href: "#settings-persona", icon: PERSONA_ICON, active: true },
              { kind: "anchor", key: "behavior", label: "Behavior", href: "#settings-behavior", icon: BEHAVIOR_ICON },
              { kind: "anchor", key: "install", label: "Install", href: "#settings-install", icon: INSTALL_ICON },
              { kind: "disabled", key: "appearance", label: "Appearance", icon: APPEARANCE_ICON },
            ] satisfies SettingsRailRow[]
          }
        />

        {/* Middle: scrolling body of set-row-styled sections */}
        <div className="flex min-w-0 flex-1 flex-col gap-5">
          <div className="scroll-mt-16 rounded-[14px] border border-[var(--line)] bg-card px-[22px] pb-2 pt-1" id="settings-persona">
            <h2 className="pt-4 pb-0.5 text-[16px] font-semibold text-foreground">Persona</h2>

            <SetRow label="Greeting message" description="Up to 2000 characters." htmlFor="greeting">
              <Textarea
                id="greeting"
                name="greeting"
                value={fields.greeting}
                onChange={(e) => setFields((f) => ({ ...f, greeting: e.target.value }))}
                maxLength={2000}
                rows={3}
                placeholder="Hi! How can I help you today?"
                className={`${SET_ROW_FIELD_CLASS} min-h-16`}
              />
              {fieldErrors.greeting ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.greeting}
                </p>
              ) : null}
            </SetRow>

            <SetRow
              label="Tone"
              description={'Free text (up to 100 characters) — e.g. "friendly", "professional", "concise". Not restricted to a fixed list.'}
              htmlFor="tone"
            >
              <input
                id="tone"
                name="tone"
                value={fields.tone}
                onChange={(e) => setFields((f) => ({ ...f, tone: e.target.value }))}
                maxLength={100}
                placeholder="friendly, professional, concise"
                className={SET_ROW_FIELD_CLASS}
              />
              {fieldErrors.tone ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.tone}
                </p>
              ) : null}
            </SetRow>

            <SetRow label="Launcher label" description={'Up to 40 characters. Leave blank to use "Chat with us".'} htmlFor="launcherLabel">
              <input
                id="launcherLabel"
                name="launcherLabel"
                value={fields.launcherLabel}
                onChange={(e) => setFields((f) => ({ ...f, launcherLabel: e.target.value }))}
                maxLength={40}
                placeholder="Chat with us"
                className={SET_ROW_FIELD_CLASS}
              />
              {fieldErrors.launcherLabel ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.launcherLabel}
                </p>
              ) : null}
            </SetRow>

            {/* sidebarWorkspaceLabel/dashboardTitle: real, already-shipped
                fields, kept here rather than under the Workspace rail entry
                (that entry anchors to the OTHER shell's real
                name/URL/timezone Workspace section, per D1). */}
            <SetRow
              label="Sidebar workspace label"
              description={'Up to 80 characters. Leave blank for "Client workspace".'}
              htmlFor="sidebarWorkspaceLabel"
            >
              <input
                id="sidebarWorkspaceLabel"
                name="sidebarWorkspaceLabel"
                value={fields.sidebarWorkspaceLabel}
                onChange={(e) => setFields((f) => ({ ...f, sidebarWorkspaceLabel: e.target.value }))}
                maxLength={80}
                placeholder="Client workspace"
                className={SET_ROW_FIELD_CLASS}
              />
              {fieldErrors.sidebarWorkspaceLabel ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.sidebarWorkspaceLabel}
                </p>
              ) : null}
            </SetRow>

            <SetRow
              label="Dashboard title"
              description={'Up to 80 characters. Leave blank for "Dashboard".'}
              htmlFor="dashboardTitle"
              isLast
            >
              <input
                id="dashboardTitle"
                name="dashboardTitle"
                value={fields.dashboardTitle}
                onChange={(e) => setFields((f) => ({ ...f, dashboardTitle: e.target.value }))}
                maxLength={80}
                placeholder="Dashboard"
                className={SET_ROW_FIELD_CLASS}
              />
              {fieldErrors.dashboardTitle ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.dashboardTitle}
                </p>
              ) : null}
            </SetRow>

            <div className="my-3 flex flex-col gap-1.5 rounded-md border border-dashed border-[var(--line-2)] bg-secondary p-3">
              <p className="text-xs font-semibold text-[var(--ink-2)]">
                Bot name &amp; suggested questions — coming soon
              </p>
              <p className="text-xs text-muted-foreground">
                These aren&apos;t configurable yet; there&apos;s no backend field for them.
              </p>
            </div>
          </div>

          <div className="scroll-mt-16 rounded-[14px] border border-[var(--line)] bg-card px-[22px] pb-2 pt-1" id="settings-behavior">
            <h2 className="pt-4 pb-0.5 text-[16px] font-semibold text-foreground">Behavior</h2>

            <SetRow label="Escalation policy" description="Up to 2000 characters." htmlFor="escalationPolicy">
              <Textarea
                id="escalationPolicy"
                name="escalationPolicy"
                value={fields.escalationPolicy}
                onChange={(e) => setFields((f) => ({ ...f, escalationPolicy: e.target.value }))}
                maxLength={2000}
                rows={3}
                placeholder="Escalate to a human agent when the visitor asks for a refund."
                className={`${SET_ROW_FIELD_CLASS} min-h-16`}
              />
              {fieldErrors.escalationPolicy ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.escalationPolicy}
                </p>
              ) : null}
            </SetRow>

            <SetRow
              label="Business hours"
              description={
                'A JSON object, e.g. {"mon": ["09:00", "17:00"]} — or leave blank. The day-picker below edits the same value as a shortcut for the common {day: [open, close]} shape; the raw JSON field stays available for anything it can\'t express.'
              }
              htmlFor="businessHoursText"
            >
              <div className="flex flex-col gap-2.5">
                <BusinessHoursPicker
                  value={fields.businessHoursText}
                  onChange={(next) => setFields((f) => ({ ...f, businessHoursText: next }))}
                />
                <Textarea
                  id="businessHoursText"
                  name="businessHoursText"
                  value={fields.businessHoursText}
                  onChange={(e) => setFields((f) => ({ ...f, businessHoursText: e.target.value }))}
                  rows={5}
                  className={`${SET_ROW_FIELD_CLASS} font-mono text-xs`}
                  placeholder={'{\n  "mon": ["09:00", "17:00"]\n}'}
                />
              </div>
              {fieldErrors.businessHoursText ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.businessHoursText}
                </p>
              ) : null}
            </SetRow>

            {/* Thresholds (B9): answer/escalate stay READ-ONLY chips per the
                reference -- PUT /admin/settings never writes them
                (page.tsx's documented read-only set). Turn cap (Tier 2) IS
                now editable from this screen -- `settings_routes.py`'s PUT
                handler writes it to `tenant_orchestrator_configs` while
                preserving the other two, so it gets a real numeric input
                instead of a non-interactive chip. */}
            <SetRow
              label="Thresholds & turn cap"
              description='Answer/escalate confidence thresholds are governed by orchestrator config and not editable here. Turn cap controls how many visitor messages the bot answers before proactively offering to connect with sales.'
              htmlFor="turnCap"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded-full bg-secondary px-3 py-1.5 text-[12px] font-semibold text-foreground">
                  Answer {currentSettings.answerThreshold}
                </span>
                <span className="rounded-full bg-secondary px-3 py-1.5 text-[12px] font-semibold text-foreground">
                  Escalate {currentSettings.escalateThreshold}
                </span>
                <label className="text-[12px] font-semibold text-foreground" htmlFor="turnCap">
                  Turn cap
                </label>
                <input
                  id="turnCap"
                  name="turnCap"
                  type="number"
                  min={1}
                  step={1}
                  inputMode="numeric"
                  value={fields.turnCap}
                  onChange={(e) => setFields((f) => ({ ...f, turnCap: e.target.value }))}
                  className={`${SET_ROW_FIELD_CLASS} w-20`}
                />
              </div>
              {fieldErrors.turnCap ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.turnCap}
                </p>
              ) : null}
            </SetRow>

            <SetRow
              label="Repeated low-confidence escalation"
              description="If the bot can't confidently answer this many questions in a row on the same topic, it stops trying to clarify and offers to connect with sales instead of waiting out the full turn cap."
              htmlFor="lowConfidenceStreakCap"
              isLast
            >
              <div className="flex flex-wrap items-center gap-2">
                <label
                  className="text-[12px] font-semibold text-foreground"
                  htmlFor="lowConfidenceStreakCap"
                >
                  Escalate after
                </label>
                <input
                  id="lowConfidenceStreakCap"
                  name="lowConfidenceStreakCap"
                  type="number"
                  min={1}
                  step={1}
                  inputMode="numeric"
                  value={fields.lowConfidenceStreakCap}
                  onChange={(e) =>
                    setFields((f) => ({ ...f, lowConfidenceStreakCap: e.target.value }))
                  }
                  className={`${SET_ROW_FIELD_CLASS} w-20`}
                />
                <span className="text-[12px] text-muted-foreground">
                  unclear replies in a row
                </span>
              </div>
              {fieldErrors.lowConfidenceStreakCap ? (
                <p role="alert" className="mt-1.5 text-sm text-destructive">
                  {fieldErrors.lowConfidenceStreakCap}
                </p>
              ) : null}
            </SetRow>
          </div>

          <div id="settings-install" className="scroll-mt-16">
            <InstallSnippet />
          </div>

          <div className="flex flex-col gap-1.5 rounded-md border border-dashed border-[var(--line-2)] bg-secondary p-3.5">
            <p className="text-xs font-semibold text-[var(--ink-2)]">
              Widget appearance — coming soon
            </p>
            <p className="text-xs text-muted-foreground">
              Accent color and launcher position swatches aren&apos;t wired to a backend field
              yet, so they aren&apos;t shown as editable controls here.
            </p>
          </div>
        </div>

        {/* SR-15 D9: 300px sticky right column holding the live preview. */}
        <aside className="flex w-full shrink-0 flex-col items-center gap-3.5 self-start rounded-[14px] border border-border bg-secondary p-6 lg:sticky lg:top-16 lg:w-[300px]">
          <WidgetPreview
            greeting={fields.greeting}
            tone={fields.tone}
            launcherLabel={fields.launcherLabel}
          />
        </aside>
      </div>
    </form>
  );
}
