/**
 * Tenant bot-settings screen (S13.6). CLIENT_ADMIN + CLIENT_AGENT can VIEW
 * (mirrors the backend's `GET /admin/settings` two-role gate); only
 * CLIENT_ADMIN gets the editable form (mirrors `PUT /admin/settings`'s
 * CLIENT_ADMIN-only gate) -- decision 2. Server-first (decision 1): this is
 * an `async` server component that loads current settings via `lib/settings`
 * `getBotSettings()` and passes them as props to a thin client form.
 *
 * SPLIT (post-SR-27, user-requested): this route used to also render the
 * workspace/account-level "Settings" shell (General/Members/Billing/API
 * keys/Notifications/Danger zone) stacked below the Bot-settings shell --
 * SR-27 slice 7/8's doc comment explicitly deferred that IA change ("If
 * splitting into two routes is wanted, that is a separate flagged decision
 * for the user, not decided here"). The user has now asked for exactly that
 * split, with its own sidebar entry -- see `/workspace` (`workspace/page.tsx`)
 * for the extracted shell, `components/admin/admin-shell.tsx`'s `toolsItems`
 * for the new nav entry, and `components/admin/{settings-rail,set-row}.tsx`
 * for the two primitives that were shared by both shells and are now shared
 * across the two routes instead. This route now renders ONLY the Bot-settings
 * shell (`Console.dc.html:500-557`).
 */
import { requireAnyRole } from "@/lib/auth";
import { getBotSettings, type BotSettings } from "@/lib/settings";
import { SoftCard } from "@/components/admin/soft-card";
import { SettingsForm } from "@/app/(protected)/settings/settings-form";

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

export default async function SettingsPage() {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const result = await getBotSettings();

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

  return <SettingsForm currentSettings={result.settings} />;
}
