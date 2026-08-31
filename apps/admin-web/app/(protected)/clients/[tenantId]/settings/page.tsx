/**
 * Per-client bot-settings screen (S13.7). Reuses S13.6's `SettingsForm`
 * as-is, parameterized by the route's `{tenantId}` (D1) so both the read
 * (`getBotSettings`) and write (`SettingsForm` -> `saveSettings`) target the
 * S12.7 PLATFORM_ADMIN super-user surface
 * `/admin/tenants/{tenantId}/settings` instead of the implicit
 * `/admin/settings`. PLATFORM_ADMIN always gets the full editable form here
 * (S12.7 D6: "everything a CLIENT_ADMIN has, plus more") -- there is no
 * read-only CLIENT_AGENT branch on this route family, since only
 * PLATFORM_ADMIN reaches `/clients/**` at all (this layout's `requireRole`
 * gate).
 */
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getBotSettings } from "@/lib/settings";
import { getCallConfig } from "@/lib/calls";
import { listKnowledgeDocs } from "@/app/(protected)/knowledge/actions";
import { SettingsForm } from "@/app/(protected)/settings/settings-form";
import { OnboardingChecklist } from "@/app/(protected)/onboarding-checklist";

export default async function ClientSettingsPage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const result = await getBotSettings(tenantId);
  const callConfigResult = await getCallConfig(tenantId);
  const docsResult = await listKnowledgeDocs(tenantId);

  return (
    <div className="flex flex-1 flex-col items-center gap-4 p-8">
      <div className="w-full max-w-2xl">
        <OnboardingChecklist
          settingsResult={result}
          docsResult={docsResult}
          callConfigResult={callConfigResult}
          settingsHref={`/clients/${tenantId}/settings`}
          knowledgeHref={`/clients/${tenantId}/knowledge`}
        />
      </div>
      <Card className="w-full max-w-2xl">
        <CardHeader>
          <CardTitle>Bot settings</CardTitle>
          <CardDescription>This client&apos;s chatbot configuration.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
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
          ) : (
            <SettingsForm
              currentSettings={result.settings}
              callConfigResult={callConfigResult}
              ownTenantId={tenantId}
              tenantId={tenantId}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
