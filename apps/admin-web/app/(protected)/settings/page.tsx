/**
 * Tenant bot-settings screen (S13.6). CLIENT_ADMIN + CLIENT_AGENT can VIEW
 * (mirrors the backend's `GET /admin/settings` two-role gate); only
 * CLIENT_ADMIN gets the editable form (mirrors `PUT /admin/settings`'s
 * CLIENT_ADMIN-only gate) -- decision 2. Server-first (decision 1): this is
 * an `async` server component that loads current settings via `lib/settings`
 * `getBotSettings()` and passes them as props to a thin client form.
 */
import Link from "next/link";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { requireAnyRole } from "@/lib/auth";
import { getBotSettings, type BotSettings } from "@/lib/settings";
import { SettingsForm } from "@/app/(protected)/settings/settings-form";

/** The five read-only fields (decision 3) -- thresholds + provider/model.
 * `PUT /admin/settings` never writes any of these; their write paths (S10.2
 * orchestrator config, and the disclosed-temporary `/debug/llm/config`) are
 * out of scope for this screen. A `null` provider/model renders as "Not
 * configured", never a fabricated default (no-silent-fallback). */
function ReadOnlyInfoPanel({ settings }: { settings: BotSettings }) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-input bg-muted/50 p-4">
      <p className="text-sm font-medium">Orchestrator &amp; LLM configuration (read-only)</p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-muted-foreground">Answer threshold</dt>
          <dd>{settings.answerThreshold}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Escalate threshold</dt>
          <dd>{settings.escalateThreshold}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Turn cap</dt>
          <dd>{settings.turnCap}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">LLM provider</dt>
          <dd>{settings.llmProvider ?? "Not configured"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">LLM model</dt>
          <dd>{settings.llmModel ?? "Not configured"}</dd>
        </div>
      </dl>
      <p className="text-xs text-muted-foreground">
        These values are not editable from this screen.
      </p>
    </div>
  );
}

/** Read-only rendering of the four qualitative fields for a CLIENT_AGENT --
 * no editable inputs, no Save button (decision 2: a clean read-only view is
 * more honest than disabled-looking inputs). */
function ReadOnlyQualitativeFields({ settings }: { settings: BotSettings }) {
  return (
    <div className="flex flex-col gap-4">
      <p role="status" className="rounded-md border border-input bg-muted/50 p-3 text-sm">
        Read-only — only a client admin can change these settings.
      </p>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Greeting</p>
        <p className="whitespace-pre-wrap text-sm">{settings.greeting || "—"}</p>
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Business hours</p>
        <pre className="overflow-x-auto rounded-md border border-input bg-muted/50 p-2 text-xs">
          {settings.businessHours ? JSON.stringify(settings.businessHours, null, 2) : "—"}
        </pre>
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Escalation policy</p>
        <p className="whitespace-pre-wrap text-sm">{settings.escalationPolicy || "—"}</p>
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Tone</p>
        <p className="text-sm">{settings.tone || "—"}</p>
      </div>
      <div className="border-t border-input pt-4">
        <p className="text-sm font-medium">Workspace</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <p className="text-xs text-muted-foreground">Sidebar workspace label</p>
            <p className="text-sm">{settings.sidebarWorkspaceLabel || "—"}</p>
          </div>
          <div className="flex flex-col gap-1">
            <p className="text-xs text-muted-foreground">Dashboard title</p>
            <p className="text-sm">{settings.dashboardTitle || "—"}</p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default async function SettingsPage() {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");

  const result = await getBotSettings();

  return (
    <div className="flex flex-1 flex-col gap-5 p-6 lg:p-8">
      <div className="flex items-center">
        <div>
          <h1 className="text-xl font-bold text-[#191a17]">Bot settings</h1>
          <p className="mt-0.5 text-[12.5px] text-[#70716a]">
            Your tenant&apos;s chatbot configuration.
          </p>
        </div>
        <Link href="/" className="ml-auto text-sm text-muted-foreground hover:underline">
          ← Back to console
        </Link>
      </div>

      {result.status === "error" ? (
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
      ) : claims.role === "CLIENT_ADMIN" ? (
        <SettingsForm currentSettings={result.settings} />
      ) : (
        <Card className="w-full max-w-2xl">
          <CardHeader>
            <CardTitle>Bot settings</CardTitle>
            <CardDescription>Your tenant&apos;s chatbot configuration.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            <ReadOnlyInfoPanel settings={result.settings} />
            <ReadOnlyQualitativeFields settings={result.settings} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
