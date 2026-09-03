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
 *
 * Bug fix: `<SettingsForm>` renders its own full-width page shell (SR-27
 * slice 8 -- a sticky header, a 184px section rail, and a 300px sticky
 * preview column via `lg:flex-row`). This screen used to wrap it in a
 * `max-w-2xl` `<Card>`, which squeezed that three-column layout into ~672px
 * on any viewport at/above the `lg` breakpoint (a VIEWPORT-width media
 * query, not the Card's own width) -- every field's input collapsed to a
 * sliver a few pixels wide as flex-shrink fought the fixed-width rail/
 * preview columns for the remaining space. `/settings/page.tsx` (the
 * client-facing host of the same component) never wrapped it this way and
 * never showed the bug. Fixed by rendering `<SettingsForm>` unwrapped here
 * too, exactly like that page does -- its own sticky "Bot settings" header
 * already covers what the removed `<Card>`'s title/description duplicated.
 */
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
    <div className="flex flex-1 flex-col gap-4 p-8">
      <div className="w-full max-w-2xl">
        <OnboardingChecklist
          settingsResult={result}
          docsResult={docsResult}
          callConfigResult={callConfigResult}
          settingsHref={`/clients/${tenantId}/settings`}
          knowledgeHref={`/clients/${tenantId}/knowledge`}
        />
      </div>
      {result.status === "error" ? (
        <p
          role="alert"
          className="w-full max-w-2xl rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
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
    </div>
  );
}
