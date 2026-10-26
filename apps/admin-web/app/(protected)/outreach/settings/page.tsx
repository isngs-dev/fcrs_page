/**
 * Outreach settings (A8 U10, CLIENT_ADMIN): kill switch, agent settings, the
 * email sender, AI calling, and the do-not-contact list.
 */
import { requireRole } from "@/lib/auth";
import {
  getAgentSettings,
  getCallAgentConfig,
  getEmailConfig,
  listSuppressions,
  setOutreachPaused,
} from "@/lib/outreach";
import { SoftCard } from "@/components/admin/soft-card";
import { ErrorCard, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";
import { ActionButton } from "@/app/(protected)/outreach/action-button";
import {
  AgentSettingsForm,
  CallConfigForm,
  EmailConfigForm,
  SuppressionsCard,
} from "@/app/(protected)/outreach/settings/settings-forms";

export default async function OutreachSettingsPage() {
  await requireRole("CLIENT_ADMIN");
  const [settings, email, call, suppressions] = await Promise.all([
    getAgentSettings(),
    getEmailConfig(),
    getCallAgentConfig(),
    listSuppressions(),
  ]);

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Outreach settings" subtitle="How Rebecca follows up by email and phone." />
      {settings.status === "error" ? (
        <ErrorCard message={settings.message} correlationId={settings.correlationId} />
      ) : (
        <>
          <SoftCard className="flex flex-wrap items-center gap-3 px-5 py-4">
            <div className="flex-1">
              <p className="text-[15px] font-semibold">
                {settings.settings.outreachPaused ? "Outreach is paused" : "Outreach is running"}
              </p>
              <p className="text-[12.5px] text-muted-foreground">
                Pausing stops every scheduled email and call right away. Nothing is lost; resume any time.
              </p>
            </div>
            <ActionButton
              action={setOutreachPaused.bind(null, !settings.settings.outreachPaused)}
              label={settings.settings.outreachPaused ? "Resume outreach" : "Pause all outreach"}
              variant={settings.settings.outreachPaused ? "default" : "destructive"}
              confirmText={settings.settings.outreachPaused ? undefined : "Pause every email and call now?"}
            />
          </SoftCard>
          <AgentSettingsForm settings={settings.settings} />
        </>
      )}
      {email.status === "error" ? (
        <ErrorCard message={email.message} correlationId={email.correlationId} />
      ) : (
        <EmailConfigForm config={email.config} />
      )}
      {call.status === "error" ? (
        <ErrorCard message={call.message} correlationId={call.correlationId} />
      ) : (
        <CallConfigForm config={call.config} />
      )}
      {suppressions.status === "error" ? (
        <ErrorCard message={suppressions.message} correlationId={suppressions.correlationId} />
      ) : (
        <SuppressionsCard items={suppressions.suppressions} />
      )}
    </div>
  );
}
