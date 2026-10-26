/**
 * Lead intake (A10, CLIENT_ADMIN): where form and Meta leads come in, how to connect
 * them, and the queue of leads that couldn't be used (never guessed -- a person decides).
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { getIntakeConfig, listIntakeQueue, resolveIntakeEvent, type IntakeEvent } from "@/lib/outreach";
import { formatWhen, humanize } from "@/lib/outreach-format";
import { SoftCard } from "@/components/admin/soft-card";
import { EmptyCard, ErrorCard, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";
import { ActionButton } from "@/app/(protected)/outreach/action-button";
import { IntakeConfigForm } from "@/app/(protected)/outreach/intake/intake-config-form";

const REASONS: Record<string, string> = {
  NO_CONTACT_DETAILS: "No email or phone number",
  INVALID_EMAIL: "The email address isn't valid",
  INVALID_PHONE: "The phone number isn't valid",
  UNTRUSTED_SENDER: "Not from a trusted sender (or the signature failed) — ignored",
  GMAIL_FORWARDING_CONFIRMATION: "Gmail asks you to confirm forwarding",
  INTAKE_DISABLED: "Intake was switched off",
  META_NOT_CONNECTED: "The Facebook page isn't connected",
};

const SOURCES: Record<string, string> = {
  web_form: "Website form",
  meta_email: "Meta e-mail",
  meta_api: "Meta Lead Ads",
};

function Url({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[12px] font-semibold text-muted-foreground">{label}</span>
      <code className="break-all rounded-[8px] bg-secondary px-2 py-1 text-[12px]">{value}</code>
    </div>
  );
}

function QueueItem({ event }: { event: IntakeEvent }) {
  const values = event.fields.values ?? {};
  const summary =
    event.fields.code
      ? `Confirmation code ${event.fields.code} — enter it in Gmail → Settings → Forwarding.`
      : Object.entries(values)
          .map(([k, v]) => `${humanize(k)}: ${v}`)
          .join(" · ") || [event.fields.from, event.fields.subject].filter(Boolean).join(" — ");
  return (
    <li className="flex flex-wrap items-center gap-3 border-b border-[var(--row-line)] py-3 last:border-b-0">
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold">{REASONS[event.reason ?? ""] ?? humanize(event.reason)}</p>
        <p className="truncate text-[12px] text-muted-foreground">
          {SOURCES[event.source] ?? event.source} · {formatWhen(event.receivedAt)}
          {summary ? ` · ${summary}` : ""}
        </p>
      </div>
      <ActionButton action={resolveIntakeEvent.bind(null, event.intakeId)} label="Resolve" variant="outline" />
    </li>
  );
}

export default async function LeadIntakePage() {
  await requireRole("CLIENT_ADMIN");
  const [config, queue] = await Promise.all([getIntakeConfig(), listIntakeQueue()]);

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader
        title="Lead intake"
        subtitle="Leads from your website forms and Meta reach Rebecca automatically — no forwarding by hand."
      />
      {config.status === "error" ? (
        <ErrorCard message={config.message} correlationId={config.correlationId} />
      ) : (
        <>
          <SoftCard className="flex flex-col gap-4 px-5 py-5">
            <h2 className="text-[16px] font-semibold">Connect your sources</h2>
            <div className="flex flex-col gap-2 text-[13px]">
              <p className="font-semibold">Website / landing-page forms</p>
              <p className="text-muted-foreground">
                In your form tool (or Zapier), send each submission to this webhook. Field names like name, email, phone,
                submission_id and call_consent are recognised; any other answers are kept on the lead.
              </p>
              <Url label="Form webhook URL (keep it private)" value={config.config.formUrl} />
            </div>
            <div className="flex flex-col gap-2 text-[13px]">
              <p className="font-semibold">Meta lead notification e-mails (Gmail)</p>
              <ol className="list-decimal pl-5 text-muted-foreground">
                <li>In SendGrid → Inbound Parse, add a host (e.g. <code>leads.yourdomain.com</code>) pointing to the URL below, with “POST the raw, full MIME message” on.</li>
                <li>In Gmail → Settings → Forwarding, add <code>rebecca@leads.yourdomain.com</code>. Gmail sends a confirmation code — it appears below; enter it in Gmail.</li>
                <li>Create a Gmail filter: from <code>facebookmail.com</code> (your lead notifications) → forward to that address.</li>
              </ol>
              <Url label="Inbound Parse URL" value={config.config.emailParseUrl} />
            </div>
            {config.config.metaWebhookUrl ? (
              <div className="flex flex-col gap-2 text-[13px]">
                <p className="font-semibold">Meta Lead Ads (direct)</p>
                <p className="text-muted-foreground">
                  Enter your Facebook page ID and a page access token below. The platform&apos;s Meta app delivers new
                  leads to:
                </p>
                <Url label="Meta webhook URL" value={config.config.metaWebhookUrl} />
              </div>
            ) : null}
          </SoftCard>
          <IntakeConfigForm config={config.config} />
        </>
      )}

      <SoftCard className="px-5 py-4">
        <h2 className="pb-1 text-[16px] font-semibold">Needs a person</h2>
        <p className="pb-2 text-[12.5px] text-muted-foreground">
          Leads Rebecca couldn&apos;t use, and messages you need to act on. Nothing here was contacted.
        </p>
        {queue.status === "error" ? (
          <p role="alert" className="text-[12.5px] text-destructive">{queue.message}</p>
        ) : queue.events.length === 0 ? (
          <EmptyCard title="All clear" body="Every lead that arrived was usable." />
        ) : (
          <ul>
            {queue.events.map((event) => (
              <QueueItem key={event.intakeId} event={event} />
            ))}
          </ul>
        )}
        <p className="pt-2 text-[12px] text-muted-foreground">
          Accepted leads appear in <Link href="/leads">Leads</Link> with their source.
        </p>
      </SoftCard>
    </div>
  );
}
