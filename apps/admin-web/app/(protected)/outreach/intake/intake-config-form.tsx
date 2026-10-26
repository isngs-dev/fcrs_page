"use client";

/** Lead intake settings (A10 I9). The Meta page token is write-only: blank keeps it. */
import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { saveIntakeConfig, type IntakeConfig } from "@/lib/outreach";

function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5 text-[12px] font-semibold">
      {label}
      {children}
      {help ? <span className="text-[11.5px] font-normal text-muted-foreground">{help}</span> : null}
    </label>
  );
}

export function IntakeConfigForm({ config }: { config: IntakeConfig }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    const text = (key: string) => String(data.get(key) ?? "").trim();
    const token = text("meta_page_token");
    setBusy(true);
    setNote(null);
    const result = await saveIntakeConfig({
      enabled: data.get("enabled") === "on",
      trusted_senders: text("trusted_senders").split(/[\s,]+/).filter(Boolean),
      form_consent_wording: text("form_consent_wording") || null,
      call_consent_wording: text("call_consent_wording") || null,
      meta_page_id: text("meta_page_id") || null,
      ...(token ? { meta_page_token: token } : {}),
    });
    setBusy(false);
    if (result.status === "error") return setNote({ ok: false, text: result.message });
    setNote({ ok: true, text: "Saved." });
    router.refresh();
  }

  return (
    <form
      className="flex flex-col gap-3 rounded-[14px] border border-[var(--line)] bg-card px-[22px] py-5"
      onSubmit={(e) => {
        e.preventDefault();
        void submit(e.currentTarget);
      }}
    >
      <h2 className="text-[16px] font-semibold">Settings</h2>
      <label className="flex items-center gap-2 text-[12.5px]">
        <input type="checkbox" name="enabled" defaultChecked={config.enabled} className="size-4" />
        Accept new leads from forms and Meta
      </label>
      <Field
        label="What your forms say about being contacted"
        help="Stored as the proof of email permission for every form lead."
      >
        <Textarea name="form_consent_wording" rows={2} maxLength={2000} defaultValue={config.formConsentWording ?? ""} className="text-[13px]" />
      </Field>
      <Field
        label="Exact call-consent wording on your forms (optional)"
        help="Only when this is filled in AND the person ticks the call box does Rebecca call them. A phone number alone is never permission to call."
      >
        <Textarea name="call_consent_wording" rows={3} maxLength={2000} defaultValue={config.callConsentWording ?? ""} className="text-[13px]" />
      </Field>
      <Field label="Trusted notification senders" help="Only lead e-mails from these domains (with a valid signature) are accepted.">
        <Input name="trusted_senders" defaultValue={config.trustedSenders.join(", ")} />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Facebook page ID (Meta Lead Ads)">
          <Input name="meta_page_id" inputMode="numeric" defaultValue={config.metaPageId ?? ""} />
        </Field>
        <Field label={config.hasMetaPageToken ? "Page access token (saved — leave blank to keep)" : "Page access token"}>
          <Input name="meta_page_token" type="password" autoComplete="off" maxLength={1000} />
        </Field>
      </div>
      {note ? (
        <p role={note.ok ? "status" : "alert"} className={note.ok ? "text-[12.5px] text-[#3f7d57]" : "text-[12.5px] text-destructive"}>
          {note.text}
        </p>
      ) : null}
      <Button type="submit" size="sm" className="self-start" disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </Button>
    </form>
  );
}
