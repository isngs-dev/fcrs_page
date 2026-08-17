/**
 * Workspace/account settings screen. Extracted from the combined `/settings`
 * route (SR-27 slice 7/8's D1 originally stacked this "Settings" shell
 * -- `Console.dc.html:858-896`, rail General/Members/Billing/API
 * keys/Notifications, General card, Danger zone -- directly beneath the
 * "Bot settings" shell on one route, with a doc comment explicitly deferring
 * a real route split as "a separate flagged decision for the user, not
 * decided here"). The user has now asked for that split, with its own
 * sidebar entry (`components/admin/admin-shell.tsx`'s `toolsItems`).
 *
 * CLIENT_ADMIN-only (unchanged from the combined route): Workspace/API-keys/
 * Availability/Danger-zone were always gated behind `claims.role ===
 * "CLIENT_ADMIN"` there, and a CLIENT_AGENT visiting `/settings` never saw
 * this shell at all (the old page returned the Bot-settings read-only view
 * before Shell 1 ever rendered). `requireRole("CLIENT_ADMIN")` here makes
 * that pre-existing behavior an explicit route-level gate instead of an
 * implicit one, matching the `/knowledge` and `/members` routes' pattern.
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { getWorkspace } from "@/lib/workspace";
import { getApiKeyInfo } from "@/lib/api-keys";
import { WorkspaceSection } from "@/app/(protected)/workspace/workspace-section";
import { ApiKeysSection } from "@/app/(protected)/workspace/api-keys-section";
import { AvailabilitySection } from "@/app/(protected)/workspace/availability-section";
import { GoogleCalendarSection } from "@/app/(protected)/workspace/google-calendar-section";
import { CalendlySection } from "@/app/(protected)/workspace/calendly-section";
import { DisabledSections } from "@/app/(protected)/workspace/disabled-sections";
import { SettingsRail, type SettingsRailRow } from "@/components/admin/settings-rail";

interface WorkspacePageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/** SETTINGS shell rail rows (`Console.dc.html:869-876`). Members and
 * Notifications are real routes -- `link` rows. Billing has no backend
 * module (G20) -- rendered `disabled`. API keys is an in-page anchor within
 * this same shell. */
const SETTINGS_RAIL_ROWS: readonly SettingsRailRow[] = [
  { kind: "link", key: "general", label: "General", href: "#settings-workspace", active: true },
  { kind: "link", key: "members", label: "Members", href: "/members" },
  { kind: "disabled", key: "billing", label: "Billing" },
  { kind: "anchor", key: "api-keys", label: "API keys", href: "#settings-api-keys" },
  { kind: "link", key: "notifications", label: "Notifications", href: "/notifications" },
];

export default async function WorkspacePage({ searchParams }: WorkspacePageProps) {
  await requireRole("CLIENT_ADMIN");

  const params = await searchParams;
  const justConnected = firstValue(params.calendar_connected) === "true";
  const callbackError = firstValue(params.calendar_error) ?? null;

  const [workspaceResult, apiKeyResult] = await Promise.all([getWorkspace(), getApiKeyInfo()]);

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex flex-none items-center justify-between border-b border-[var(--line)] px-[30px] py-4 pb-[16px]">
        <div>
          <h1 className="text-[24px] font-semibold text-foreground">Settings</h1>
          <p className="mt-[3px] text-[12.5px] text-muted-foreground">
            Manage your workspace, members, and integrations.
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <Link
            href="/"
            className="flex h-[38px] items-center rounded-[10px] border border-[var(--line)] bg-card px-4 text-[13.5px] font-semibold text-[var(--ink-2)] hover:bg-secondary"
          >
            Discard
          </Link>
          <button
            type="submit"
            form="workspace-form"
            className="flex h-[38px] items-center rounded-[10px] bg-primary px-4 text-[13.5px] font-semibold text-primary-foreground hover:bg-primary/90"
          >
            Save changes
          </button>
        </div>
      </div>

      <div className="flex flex-1 min-h-0">
        <SettingsRail eyebrow="Settings" rows={SETTINGS_RAIL_ROWS} />

        <div className="flex min-w-0 flex-1 flex-col gap-5 overflow-y-auto px-[26px] py-6">
          {workspaceResult.status === "ok" ? (
            <WorkspaceSection currentWorkspace={workspaceResult.workspace} />
          ) : (
            <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
              {workspaceResult.message}
            </p>
          )}

          {apiKeyResult.status === "ok" ? (
            <ApiKeysSection currentInfo={apiKeyResult.info} />
          ) : (
            <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
              {apiKeyResult.message}
            </p>
          )}

          {/* Tier 2 (sales-call-booking flow fix): scheduling availability
              -- timezone/weekly hours/slot length/buffer -- via
              PUT /admin/schedule/availability. CLIENT_ADMIN-only, same as
              Workspace/API keys above. */}
          <AvailabilitySection />

          {/* SR-22: Google Calendar connect trigger -- sits right below
              availability since a connected calendar is what turns a booked
              slot into a real event with a Meet link. */}
          <GoogleCalendarSection justConnected={justConnected} callbackError={callbackError} />

          <CalendlySection />

          <DisabledSections />
        </div>
      </div>
    </div>
  );
}
