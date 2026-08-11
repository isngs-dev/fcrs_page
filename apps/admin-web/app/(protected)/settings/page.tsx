/**
 * Tenant bot-settings screen (S13.6). CLIENT_ADMIN + CLIENT_AGENT can VIEW
 * (mirrors the backend's `GET /admin/settings` two-role gate); only
 * CLIENT_ADMIN gets the editable form (mirrors `PUT /admin/settings`'s
 * CLIENT_ADMIN-only gate) -- decision 2. Server-first (decision 1): this is
 * an `async` server component that loads current settings via `lib/settings`
 * `getBotSettings()` and passes them as props to a thin client form.
 *
 * SR-27 slice 7/8 (D1, LOCKED for this sprint): there is exactly ONE
 * `/settings` route, and the reference has TWO distinct artboards --
 * "Settings" (`Console.dc.html:858-896`: rail General/Members/Billing/API
 * keys/Notifications, General card, Danger zone) and "Bot settings"
 * (`Console.dc.html:500-557`: rail Persona/Behavior/Workspace/Install/
 * Appearance, Persona/Behavior cards, live preview). Splitting into two
 * real routes is an information-architecture change (breaks bookmarks,
 * touches the sidebar nav, the `clients/[tenantId]` platform-admin twin,
 * and every existing settings test) far beyond a fidelity rebuild -- the
 * handoff's decision is to render BOTH reference layouts as two stacked
 * shells in this one route, each with its own 184px `.setnav` rail and its
 * own sticky header. If splitting into two routes is wanted, that is a
 * separate flagged decision for the user, not decided here.
 */
import Link from "next/link";
import { SoftCard } from "@/components/admin/soft-card";
import { requireAnyRole } from "@/lib/auth";
import { getBotSettings, type BotSettings } from "@/lib/settings";
import { getWorkspace } from "@/lib/workspace";
import { getApiKeyInfo } from "@/lib/api-keys";
import { SettingsForm } from "@/app/(protected)/settings/settings-form";
import { WorkspaceSection } from "@/app/(protected)/settings/workspace-section";
import { ApiKeysSection } from "@/app/(protected)/settings/api-keys-section";
import { DisabledSections } from "@/app/(protected)/settings/disabled-sections";
import { SettingsRail, type SettingsRailRow } from "@/app/(protected)/settings/settings-rail";

/** The five read-only fields (decision 3) -- thresholds + provider/model.
 * `PUT /admin/settings` never writes any of these; their write paths (S10.2
 * orchestrator config, and the disclosed-temporary `/debug/llm/config`) are
 * out of scope for this screen. A `null` provider/model renders as "Not
 * configured", never a fabricated default (no-silent-fallback). */
function ReadOnlyInfoPanel({ settings }: { settings: BotSettings }) {
  return (
    <SoftCard className="flex flex-col gap-3 bg-secondary p-4">
      <p className="text-sm font-semibold text-foreground">Orchestrator &amp; LLM configuration (read-only)</p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-muted-foreground">Answer threshold</dt>
          <dd className="text-foreground">{settings.answerThreshold}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Escalate threshold</dt>
          <dd className="text-foreground">{settings.escalateThreshold}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Turn cap</dt>
          <dd className="text-foreground">{settings.turnCap}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">LLM provider</dt>
          <dd className="text-foreground">{settings.llmProvider ?? "Not configured"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">LLM model</dt>
          <dd className="text-foreground">{settings.llmModel ?? "Not configured"}</dd>
        </div>
      </dl>
      <p className="text-xs text-muted-foreground">
        These values are not editable from this screen.
      </p>
    </SoftCard>
  );
}

/** Read-only rendering of the four qualitative fields for a CLIENT_AGENT --
 * no editable inputs, no Save button (decision 2: a clean read-only view is
 * more honest than disabled-looking inputs). */
function ReadOnlyQualitativeFields({ settings }: { settings: BotSettings }) {
  return (
    <div className="flex flex-col gap-4">
      <p role="status" className="rounded-[10px] border border-border bg-secondary p-3 text-sm text-[var(--ink-2)]">
        Read-only — only a client admin can change these settings.
      </p>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Greeting</p>
        <p className="whitespace-pre-wrap text-sm text-foreground">{settings.greeting || "—"}</p>
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Business hours</p>
        <pre className="overflow-x-auto rounded-[10px] border border-border bg-secondary p-2 text-xs text-foreground">
          {settings.businessHours ? JSON.stringify(settings.businessHours, null, 2) : "—"}
        </pre>
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Escalation policy</p>
        <p className="whitespace-pre-wrap text-sm text-foreground">{settings.escalationPolicy || "—"}</p>
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Tone</p>
        <p className="text-sm text-foreground">{settings.tone || "—"}</p>
      </div>
      <div className="border-t border-border pt-4">
        <p className="text-sm font-semibold text-foreground">Workspace</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <p className="text-xs text-muted-foreground">Sidebar workspace label</p>
            <p className="text-sm text-foreground">{settings.sidebarWorkspaceLabel || "—"}</p>
          </div>
          <div className="flex flex-col gap-1">
            <p className="text-xs text-muted-foreground">Dashboard title</p>
            <p className="text-sm text-foreground">{settings.dashboardTitle || "—"}</p>
          </div>
        </div>
      </div>
    </div>
  );
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

export default async function SettingsPage() {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const result = await getBotSettings();
  const isClientAdmin = claims.role === "CLIENT_ADMIN";
  // Workspace + API-keys are CLIENT_ADMIN-only surfaces (SR-20 D5/D6,
  // tenant-wide config); only fetched/rendered for that role -- an agent
  // never sees them (admin-web skill: hide what the API forbids).
  const workspaceResult = isClientAdmin ? await getWorkspace() : null;
  const apiKeyResult = isClientAdmin ? await getApiKeyInfo() : null;

  if (result.status === "error") {
    return (
      <div className="flex flex-1 flex-col gap-5 p-6 lg:p-8">
        <p
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
        >
          {result.message}
          {result.correlationId ? (
            <span className="block text-xs text-destructive/80">
              Correlation ID: {result.correlationId}
            </span>
          ) : null}
        </p>
      </div>
    );
  }

  if (claims.role !== "CLIENT_ADMIN") {
    return (
      <div className="flex flex-1 flex-col gap-5 p-6 lg:p-8">
        <SoftCard className="flex w-full max-w-2xl flex-col gap-6 p-6">
          <div>
            <p className="text-lg font-bold text-foreground">Bot settings</p>
            <p className="mt-0.5 text-sm text-muted-foreground">Your tenant&apos;s chatbot configuration.</p>
          </div>
          <ReadOnlyInfoPanel settings={result.settings} />
          <ReadOnlyQualitativeFields settings={result.settings} />
        </SoftCard>
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col">
      {/* ============ SHELL 1: SETTINGS (Console.dc.html:858-896) ============ */}
      <div className="flex flex-1 flex-col border-b border-[var(--line)]">
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
            {workspaceResult?.status === "ok" ? (
              <WorkspaceSection currentWorkspace={workspaceResult.workspace} />
            ) : workspaceResult?.status === "error" ? (
              <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
                {workspaceResult.message}
              </p>
            ) : null}

            {apiKeyResult?.status === "ok" ? (
              <ApiKeysSection currentInfo={apiKeyResult.info} />
            ) : apiKeyResult?.status === "error" ? (
              <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
                {apiKeyResult.message}
              </p>
            ) : null}

            <DisabledSections />
          </div>
        </div>
      </div>

      {/* ============ SHELL 2: BOT SETTINGS (Console.dc.html:500-557) ============ */}
      <SettingsForm currentSettings={result.settings} />
    </div>
  );
}
