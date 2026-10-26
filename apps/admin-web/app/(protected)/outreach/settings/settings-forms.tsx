"use client";

/**
 * Outreach settings forms (A8 U10). Each card saves on its own; secrets
 * (SendGrid key, Plivo token) are write-only -- blank keeps the stored one.
 */
import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  addSuppression,
  removeSuppression,
  saveAgentSettings,
  saveCallAgentConfig,
  saveEmailConfig,
  type AgentSettings,
  type CallConfig,
  type EmailConfig,
  type Suppression,
} from "@/lib/outreach";
import { dollarsToMicros, formatWhen, humanize, microsToDollars } from "@/lib/outreach-format";

type Note = { ok: boolean; text: string } | null;

function Card({ title, help, children }: { title: string; help: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 rounded-[14px] border border-[var(--line)] bg-card px-[22px] py-5">
      <h2 className="text-[16px] font-semibold">{title}</h2>
      <p className="text-[12.5px] text-muted-foreground">{help}</p>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5 text-[12px] font-semibold">
      {label}
      {children}
    </label>
  );
}

function Check({ name, label, defaultChecked }: { name: string; label: string; defaultChecked?: boolean }) {
  return (
    <label className="flex items-center gap-2 text-[12.5px]">
      <input type="checkbox" name={name} defaultChecked={defaultChecked} className="size-4" />
      {label}
    </label>
  );
}

function Status({ note }: { note: Note }) {
  if (!note) return null;
  return (
    <p role={note.ok ? "status" : "alert"} className={note.ok ? "text-[12.5px] text-[#3f7d57]" : "text-[12.5px] text-destructive"}>
      {note.text}
    </p>
  );
}

/** Shared submit plumbing: build the body, save, show the result, refresh. */
function useSave() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note>(null);
  async function save(run: () => Promise<{ status: "ok" } | { status: "error"; message: string }>) {
    setBusy(true);
    setNote(null);
    const result = await run();
    setBusy(false);
    if (result.status === "error") return setNote({ ok: false, text: result.message });
    setNote({ ok: true, text: "Saved." });
    router.refresh();
  }
  return { busy, note, setNote, save };
}

const text = (data: FormData, key: string) => String(data.get(key) ?? "").trim();
const optional = (data: FormData, key: string) => text(data, key) || null;

// -- Agent settings ---------------------------------------------------------------

export function AgentSettingsForm({ settings }: { settings: AgentSettings }) {
  const { busy, note, setNote, save } = useSave();

  function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    const cap = dollarsToMicros(text(data, "cap"));
    if (cap === undefined) return setNote({ ok: false, text: "Monthly cap must be a dollar amount, e.g. 150 or 150.00." });
    void save(() =>
      saveAgentSettings({
        enabled_channels: ["email", "call"].filter((ch) => data.get(`channel_${ch}`) === "on"),
        email_mode: text(data, "email_mode"),
        region_profile: "US",
        call_window_start: text(data, "call_window_start"),
        call_window_end: text(data, "call_window_end"),
        email_window_start: text(data, "email_window_start"),
        email_window_end: text(data, "email_window_end"),
        max_touches_per_contact: Number(text(data, "max_touches")),
        max_call_attempts: Number(text(data, "max_call_attempts")),
        min_hours_between_touches: Number(text(data, "min_hours")),
        first_inbound_email_delay_minutes: Number(text(data, "first_delay")),
        monthly_cost_cap_micros: cap,
        physical_address: optional(data, "physical_address"),
        inbound_auto_enabled: data.get("inbound_auto") === "on",
      })
    );
  }

  return (
    <Card title="Agent settings" help="Which channels Rebecca may use, when she may call, and the limits she works within.">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(e.currentTarget);
        }}
      >
        <div className="flex flex-wrap gap-4">
          <Check name="channel_email" label="Email follow-up" defaultChecked={settings.enabledChannels.includes("email")} />
          <Check name="channel_call" label="AI calls" defaultChecked={settings.enabledChannels.includes("call")} />
          <Check name="inbound_auto" label="Follow up new chat leads automatically" defaultChecked={settings.inboundAutoEnabled} />
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Emails">
            <select name="email_mode" defaultValue={settings.emailMode} className="h-9 rounded-[10px] border border-border bg-background px-2 text-[13px]">
              <option value="draft_for_review">Draft for review (a person approves)</option>
              <option value="auto_send">Send automatically</option>
            </select>
          </Field>
          <Field label="Call from (their local time)">
            <Input name="call_window_start" type="time" defaultValue={settings.callWindowStart} required />
          </Field>
          <Field label="Call until">
            <Input name="call_window_end" type="time" defaultValue={settings.callWindowEnd} required />
          </Field>
          <Field label="Follow-up emails from (their local time)">
            <Input name="email_window_start" type="time" defaultValue={settings.emailWindowStart} required />
          </Field>
          <Field label="Follow-up emails until">
            <Input name="email_window_end" type="time" defaultValue={settings.emailWindowEnd} required />
          </Field>
          <Field label="Max touches per contact">
            <Input name="max_touches" type="number" min={1} max={10} defaultValue={settings.maxTouchesPerContact} required />
          </Field>
          <Field label="Max call attempts">
            <Input name="max_call_attempts" type="number" min={1} max={5} defaultValue={settings.maxCallAttempts} required />
          </Field>
          <Field label="Hours between touches">
            <Input name="min_hours" type="number" min={1} max={720} defaultValue={settings.minHoursBetweenTouches} required />
          </Field>
          <Field label="First email after a chat (minutes)">
            <Input name="first_delay" type="number" min={0} max={1440} defaultValue={settings.firstInboundEmailDelayMinutes} required />
          </Field>
          <Field label="Monthly cost cap ($, blank = none)">
            <Input name="cap" inputMode="decimal" defaultValue={microsToDollars(settings.monthlyCostCapMicros)} />
          </Field>
        </div>
        <Field label="Business postal address (shown in every email footer)">
          <Input name="physical_address" maxLength={500} defaultValue={settings.physicalAddress ?? ""} />
        </Field>
        <Status note={note} />
        <Button type="submit" size="sm" className="self-start" disabled={busy}>
          {busy ? "Saving…" : "Save settings"}
        </Button>
      </form>
    </Card>
  );
}

// -- Email sender -----------------------------------------------------------------

export function EmailConfigForm({ config }: { config: EmailConfig }) {
  const { busy, note, save } = useSave();

  function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    const apiKey = text(data, "api_key");
    void save(() =>
      saveEmailConfig({
        provider: text(data, "provider"),
        from_address: text(data, "from_address"),
        from_name: text(data, "from_name"),
        reply_to: optional(data, "reply_to"),
        booking_url: optional(data, "booking_url"),
        reply_domain: optional(data, "reply_domain"),
        // Blank keeps the stored key (backend A8 rule); only a new value replaces it.
        ...(text(data, "event_key") ? { event_webhook_public_key: text(data, "event_key") } : {}),
        enabled: data.get("enabled") === "on",
        ...(apiKey ? { api_key: apiKey } : {}),
      })
    );
  }

  return (
    <Card title="Email sender" help="Rebecca sends from your domain through SendGrid. Replies come back through SendGrid Inbound Parse.">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(e.currentTarget);
        }}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Provider">
            <select name="provider" defaultValue={config.provider ?? "sendgrid"} className="h-9 rounded-[10px] border border-border bg-background px-2 text-[13px]">
              <option value="sendgrid">SendGrid</option>
              <option value="smtp">SMTP (server default)</option>
            </select>
          </Field>
          <Field label={config.hasApiKey ? "SendGrid API key (saved — leave blank to keep)" : "SendGrid API key"}>
            <Input name="api_key" type="password" autoComplete="off" maxLength={500} />
          </Field>
          <Field label="From address">
            <Input name="from_address" type="email" required defaultValue={config.fromAddress ?? ""} />
          </Field>
          <Field label="Business name">
            <Input name="from_name" required maxLength={80} defaultValue={config.fromName ?? ""} />
          </Field>
          <Field label="Reply-to (optional)">
            <Input name="reply_to" type="email" defaultValue={config.replyTo ?? ""} />
          </Field>
          <Field label="Booking link (optional)">
            <Input name="booking_url" type="url" defaultValue={config.bookingUrl ?? ""} />
          </Field>
          <Field label="Reply domain (e.g. reply.yourdomain.com)">
            <Input name="reply_domain" defaultValue={config.replyDomain ?? ""} />
          </Field>
          <Field label={config.hasEventKey ? "Event Webhook key (saved)" : "Event Webhook verification key"}>
            <Input name="event_key" autoComplete="off" maxLength={500} />
          </Field>
        </div>
        <Check name="enabled" label="Email sender enabled" defaultChecked={config.enabled ?? true} />
        {config.inboundParseUrl ? (
          <div className="rounded-[10px] bg-secondary px-4 py-3 text-[12px]">
            <p className="font-semibold">Paste these into SendGrid:</p>
            <p className="break-all">Inbound Parse URL: {config.inboundParseUrl}</p>
            <p className="break-all">Event Webhook URL: {config.eventWebhookUrl}</p>
          </div>
        ) : null}
        <Status note={note} />
        <Button type="submit" size="sm" className="self-start" disabled={busy}>
          {busy ? "Saving…" : "Save email sender"}
        </Button>
      </form>
    </Card>
  );
}

// -- Calling ----------------------------------------------------------------------

export function CallConfigForm({ config }: { config: CallConfig }) {
  const { busy, note, save } = useSave();

  function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    const token = text(data, "auth_token");
    void save(() =>
      saveCallAgentConfig({
        enabled: data.get("enabled") === "on",
        plivo_auth_id: text(data, "auth_id"),
        from_number: text(data, "from_number"),
        transfer_number: optional(data, "transfer_number"),
        company_name: text(data, "company_name"),
        voicemail_policy: text(data, "voicemail_policy"),
        voicemail_message: optional(data, "voicemail_message"),
        max_call_seconds: Number(text(data, "max_call_seconds")),
        max_concurrent_calls: Number(text(data, "max_concurrent")),
        recording_enabled: data.get("recording") === "on",
        inbound_enabled: data.get("inbound") === "on",
        recording_retention_days: Number(text(data, "retention_days")),
        // Not editable here; send the stored values back so a save never resets them.
        voice: config.voice ?? "Polly.Joanna-Neural",
        voice_model: config.voiceModel ?? null,
        ...(token ? { plivo_auth_token: token } : {}),
      })
    );
  }

  return (
    <Card title="AI calling (Plivo)" help="Rebecca always says she's an AI assistant and, when recording, that the call is recorded.">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(e.currentTarget);
        }}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Plivo Auth ID">
            <Input name="auth_id" required minLength={5} maxLength={64} defaultValue={config.plivoAuthId ?? ""} autoComplete="off" />
          </Field>
          <Field label={config.configured ? "Plivo Auth Token (saved — leave blank to keep)" : "Plivo Auth Token"}>
            <Input name="auth_token" type="password" autoComplete="off" maxLength={200} required={!config.configured} />
          </Field>
          <Field label="Your Plivo number">
            <Input name="from_number" type="tel" required defaultValue={config.fromNumber ?? ""} />
          </Field>
          <Field label="Transfer to (a person, optional)">
            <Input name="transfer_number" type="tel" defaultValue={config.transferNumber ?? ""} />
          </Field>
          <Field label="Business name (spoken)">
            <Input name="company_name" required maxLength={80} defaultValue={config.companyName ?? ""} />
          </Field>
          <Field label="Voicemail">
            <select name="voicemail_policy" defaultValue={config.voicemailPolicy ?? "hangup"} className="h-9 rounded-[10px] border border-border bg-background px-2 text-[13px]">
              <option value="hangup">Hang up, try again later</option>
              <option value="leave_message">Leave the message below</option>
            </select>
          </Field>
          <Field label="Longest call (seconds)">
            <Input name="max_call_seconds" type="number" min={60} max={1800} defaultValue={config.maxCallSeconds ?? 300} required />
          </Field>
          <Field label="Calls at the same time">
            <Input name="max_concurrent" type="number" min={1} max={20} defaultValue={config.maxConcurrentCalls ?? 2} required />
          </Field>
          <Field label="Delete recordings after (days)">
            <Input name="retention_days" type="number" min={1} max={3650} defaultValue={config.recordingRetentionDays ?? 90} required />
          </Field>
        </div>
        <Field label="Voicemail message (must say it's an AI assistant, your business name and a callback number)">
          <Textarea name="voicemail_message" rows={3} maxLength={600} defaultValue={config.voicemailMessage ?? ""} className="text-[13px]" />
        </Field>
        <div className="flex flex-wrap gap-4">
          <Check name="enabled" label="Calling enabled" defaultChecked={config.enabled ?? true} />
          <Check name="recording" label="Record calls (announced first)" defaultChecked={config.recordingEnabled ?? true} />
          <Check name="inbound" label="Answer calls to this number" defaultChecked={config.inboundEnabled ?? true} />
        </div>
        {config.inboundAnswerUrl ? (
          <div className="rounded-[10px] bg-secondary px-4 py-3 text-[12px]">
            <p className="font-semibold">In Plivo, create an Application with these URLs (POST) and attach your number:</p>
            <p className="break-all">Answer URL: {config.inboundAnswerUrl}</p>
            <p className="break-all">Hangup URL: {config.inboundHangupUrl}</p>
          </div>
        ) : null}
        <Status note={note} />
        <Button type="submit" size="sm" className="self-start" disabled={busy}>
          {busy ? "Saving…" : "Save calling"}
        </Button>
      </form>
    </Card>
  );
}

// -- Do-not-contact list ------------------------------------------------------------

export function SuppressionsCard({ items }: { items: Suppression[] }) {
  const { busy, note, save } = useSave();

  function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    void save(async () => {
      const result = await addSuppression(text(data, "channel"), text(data, "value"));
      if (result.status === "ok") form.reset();
      return result;
    });
  }

  return (
    <Card title="Do-not-contact list" help="Rebecca never emails or calls anyone on this list. Opt-outs and bounces are added automatically.">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          submit(e.currentTarget);
        }}
      >
        <select name="channel" aria-label="Channel" className="h-9 rounded-[10px] border border-border bg-background px-2 text-[13px]">
          <option value="all">All channels</option>
          <option value="email">Email</option>
          <option value="call">Calls</option>
        </select>
        <Input name="value" required minLength={3} maxLength={320} placeholder="Email or phone number" aria-label="Email or phone" className="max-w-[280px]" />
        <Button type="submit" size="sm" disabled={busy}>
          Add
        </Button>
      </form>
      <Status note={note} />
      {items.length === 0 ? (
        <p className="text-[12.5px] text-muted-foreground">Nobody on the list yet.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-[var(--row-line)] text-[13px]">
          {items.map((s) => (
            <li key={s.suppressionId} className="flex items-center gap-3 py-2">
              <span className="min-w-0 flex-1 truncate font-medium">{s.value}</span>
              <span className="text-muted-foreground">
                {humanize(s.channel)} · {humanize(s.reason)} · {formatWhen(s.createdAt)}
              </span>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  if (window.confirm(`Allow contacting ${s.value} again?`)) void save(() => removeSuppression(s.suppressionId));
                }}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
