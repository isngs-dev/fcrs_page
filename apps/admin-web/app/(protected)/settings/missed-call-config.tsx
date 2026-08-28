"use client";

/**
 * Missed-call text-back config -- a self-contained card (own state, own save
 * call), NOT wired into `settings-form.tsx`'s big `useActionState` form,
 * mirroring `test-bot-chat.tsx`/`coverage-gaps.tsx`'s pattern of a
 * standalone client component calling its own server function directly.
 *
 * The webhook URL is constructed client-side from `WIDGET_SCRIPT_SRC`-style
 * constants, exactly like `install-snippet.tsx`'s own approach -- there is
 * no backend "public API base URL" setting to read instead, and this
 * mirrors the one existing precedent in this codebase rather than inventing
 * a new one.
 */
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { saveCallConfig, type CallConfigResult } from "@/lib/calls";

const DEFAULT_API_BASE = "https://api.chatleads.io";

function buildWebhookUrl(tenantId: string): string {
  return `${DEFAULT_API_BASE}/public/calls/twilio/${tenantId}`;
}

export function MissedCallConfig({
  result,
  ownTenantId,
  tenantId,
}: {
  result: CallConfigResult;
  /** The tenant this config belongs to -- ALWAYS set, used only to build the
   * webhook URL to display. Distinct from `tenantId` below. */
  ownTenantId: string;
  /** PLATFORM_ADMIN super-user save-path target (S12.7 convention) --
   * `undefined` on the implicit CLIENT_ADMIN route. */
  tenantId?: string;
}) {
  const phoneId = useId();
  const messageId = useId();
  const initialConfig = result.status === "ok" ? result.config : null;
  const [phone, setPhone] = useState(initialConfig?.monitoredPhoneNumber ?? "");
  const [enabled, setEnabled] = useState(initialConfig?.enabled ?? false);
  const [message, setMessage] = useState(initialConfig?.textBackMessage ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState(false);

  const webhookUrl = buildWebhookUrl(ownTenantId);

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSaved(false);
    const result = await saveCallConfig(phone.trim(), enabled, message.trim(), tenantId);
    setSaving(false);
    if (result.status === "error") {
      setError(result.message);
      return;
    }
    setSaved(true);
  }

  async function handleCopy() {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(webhookUrl);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      } catch {
        // Silent -- the URL is still visible/selectable in the field itself.
      }
    }
  }

  return (
    <div className="scroll-mt-16 rounded-[14px] border border-[var(--line)] bg-card px-[22px] pb-5 pt-1">
      <h2 className="pt-4 pb-0.5 text-[16px] font-semibold text-foreground">
        Missed-call text-back
      </h2>
      <p className="pb-3 text-[12.5px] text-muted-foreground">
        When a visitor calls your business number and nobody answers, automatically text them
        back so they don&apos;t just give up. Requires SMS (Twilio) already configured in your
        notification settings.
      </p>

      {result.status === "error" ? (
        <p role="alert" className="pb-3 text-[12.5px] text-destructive">
          Unable to load this setting. {result.message}
        </p>
      ) : null}

      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-1.5">
          <label htmlFor={phoneId} className="text-[12px] font-semibold text-foreground">
            Business phone number to watch
          </label>
          <input
            id={phoneId}
            type="tel"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+15551234567"
            className="h-10 rounded-[9px] border border-[var(--line)] px-3 text-[13px] text-foreground"
          />
        </div>

        <label className="flex items-center gap-2 text-[12.5px] font-medium text-foreground">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="size-4"
          />
          Enabled
        </label>

        <div className="flex flex-col gap-1.5">
          <label htmlFor={messageId} className="text-[12px] font-semibold text-foreground">
            Text-back message
          </label>
          <Textarea
            id={messageId}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Sorry we missed your call! Chat with us here: https://yoursite.com"
            rows={3}
            className="text-[13px]"
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <p className="text-[12px] font-semibold text-foreground">Webhook URL</p>
          <div className="flex items-center justify-between gap-2.5 rounded-[10px] bg-primary px-3.5 py-3">
            <code className="min-w-0 flex-1 overflow-x-auto font-mono text-xs whitespace-pre text-primary-foreground/80">
              {webhookUrl}
            </code>
            <Button
              type="button"
              size="sm"
              onClick={() => void handleCopy()}
              className="shrink-0 bg-primary-foreground text-primary hover:bg-primary-foreground/90"
            >
              {copied ? "Copied!" : "Copy"}
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            Paste this into your Twilio number&apos;s &quot;Call Status Changes&quot; webhook
            (A call comes in, HTTP POST).
          </p>
        </div>

        {error ? (
          <p role="alert" className="text-[12.5px] text-destructive">
            {error}
          </p>
        ) : null}
        {saved ? (
          <p role="status" className="text-[12.5px] font-medium text-[#3f7d57]">
            Saved.
          </p>
        ) : null}

        <Button type="button" size="sm" onClick={() => void handleSave()} disabled={saving} className="self-start">
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}
