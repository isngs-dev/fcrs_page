"use client";

/**
 * The three A5 ways to start outreach (A8 U9). Each has an explicit confirm
 * step -- an upload is only a preview until "Start", a Rebecca request is only
 * a card until "Confirm". Nothing launches on the first click.
 */
import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  addContact,
  commitImport,
  confirmCommand,
  createCommand,
  discardCommand,
  discardImport,
  uploadImport,
  type CommandDraft,
  type ImportPreview,
} from "@/lib/outreach";
import { humanize } from "@/lib/outreach-format";

const card = "flex flex-col gap-3 rounded-[14px] border border-[var(--line)] bg-card px-[22px] py-5";

function Note({ note }: { note: { ok: boolean; text: string } | null }) {
  if (!note) return null;
  return (
    <p role={note.ok ? "status" : "alert"} className={note.ok ? "text-[12.5px] text-[#3f7d57]" : "text-[12.5px] text-destructive"}>
      {note.text}
    </p>
  );
}

/** Import / request result keys (camelized backend counters) in plain words. */
const RESULT_LABELS: Record<string, string> = {
  contactCreated: "new contacts",
  contactExisting: "already in your contacts",
  planPlanned: "follow-ups started",
  planned: "follow-ups started",
  planNoAllowedChannel: "no channel allowed yet (no consent)",
  noAllowedChannel: "no channel allowed yet (no consent)",
  planPlanAlreadyActive: "already being followed up",
  planAlreadyActive: "already being followed up",
  planNotStarted: "added without follow-up",
  stopped: "follow-ups stopped",
  noActivePlan: "had no follow-up",
  contactGone: "contact no longer exists",
};

function counts(record: Record<string, number> | undefined): string {
  return Object.entries(record ?? {})
    .map(([k, v]) => `${v} ${RESULT_LABELS[k] ?? humanize(k).toLowerCase()}`)
    .join(" · ");
}

// -- CSV import ---------------------------------------------------------------------

export function ImportCard() {
  const router = useRouter();
  const fileId = useId();
  const nameId = useId();
  const goalId = useId();
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [attested, setAttested] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function upload(form: HTMLFormElement) {
    setBusy(true);
    setNote(null);
    const result = await uploadImport(new FormData(form));
    setBusy(false);
    if (result.status === "error") return setNote({ ok: false, text: result.message });
    setPreview(result.preview);
    setAttested(false);
  }

  async function commit() {
    if (!preview) return;
    setBusy(true);
    const result = await commitImport(preview.importId, attested, true);
    setBusy(false);
    if (result.status === "error") return setNote({ ok: false, text: result.message });
    setNote({ ok: true, text: `Started. ${counts(result.result.report.committed)}` });
    setPreview(null);
    router.refresh();
  }

  async function discard() {
    if (!preview) return;
    await discardImport(preview.importId);
    setPreview(null);
    setNote({ ok: true, text: "Import discarded." });
  }

  return (
    <div className={card}>
      <h2 className="text-[16px] font-semibold">Import a contact list (CSV)</h2>
      <p className="text-[12.5px] text-muted-foreground">
        Columns: name, email, phone, timezone, and for AI calls the consent evidence — call_consent, consent_date,
        consent_source, consent_wording, consent_type. Rows without written call consent get email only.
      </p>
      {!preview ? (
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void upload(e.currentTarget);
          }}
        >
          <label htmlFor={nameId} className="text-[12px] font-semibold">
            Campaign name
          </label>
          <Input id={nameId} name="campaign_name" required maxLength={120} placeholder="Spring roof check-ups" />
          <label htmlFor={goalId} className="text-[12px] font-semibold">
            Goal (optional)
          </label>
          <Input id={goalId} name="goal" maxLength={200} placeholder="Book a free inspection" />
          <label htmlFor={fileId} className="text-[12px] font-semibold">
            CSV file (up to 5,000 rows)
          </label>
          <input id={fileId} name="file" type="file" accept=".csv,text/csv" required className="text-[13px]" />
          <Button type="submit" size="sm" className="self-start" disabled={busy}>
            {busy ? "Checking…" : "Upload and preview"}
          </Button>
        </form>
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-[13px]">
            <strong>{preview.campaignName}</strong> — {preview.report.total} rows: {preview.report.valid} valid (
            {preview.report.new} new, {preview.report.existing} already in your contacts), {preview.report.rejectedCount}{" "}
            rejected, {preview.report.callConsentRows} with call consent.
          </p>
          {preview.report.rejectedCount ? (
            <div className="text-[12.5px]">
              <ul className="list-disc pl-5">
                {preview.report.rejected.slice(0, 10).map((r) => (
                  <li key={r.row}>
                    Row {r.row}: {r.reasons.map(humanize).join(", ")}
                  </li>
                ))}
              </ul>
              <a href={`/outreach/campaigns/imports/${encodeURIComponent(preview.importId)}/rejected`}>
                Download rejected rows (CSV)
              </a>
            </div>
          ) : null}
          {preview.report.unknownColumns.length ? (
            <p className="text-[12px] text-muted-foreground">Ignored columns: {preview.report.unknownColumns.join(", ")}</p>
          ) : null}
          {preview.report.callConsentRows ? (
            <label className="flex items-start gap-2 text-[12.5px]">
              <input type="checkbox" className="mt-0.5 size-4" checked={attested} onChange={(e) => setAttested(e.target.checked)} />
              I confirm the call-consent evidence in this file is genuine and was given to call these people with an AI
              assistant.
            </label>
          ) : null}
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              disabled={busy || preview.report.valid === 0 || (preview.report.callConsentRows > 0 && !attested)}
              onClick={() => void commit()}
            >
              {busy ? "Starting…" : `Start outreach for ${preview.report.valid}`}
            </Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void discard()}>
              Discard
            </Button>
          </div>
        </div>
      )}
      <Note note={note} />
    </div>
  );
}

const US_ZONES: [string, string][] = [
  ["America/New_York", "Eastern"],
  ["America/Chicago", "Central"],
  ["America/Denver", "Mountain"],
  ["America/Phoenix", "Arizona"],
  ["America/Los_Angeles", "Pacific"],
  ["America/Anchorage", "Alaska"],
  ["Pacific/Honolulu", "Hawaii"],
];

// -- One contact --------------------------------------------------------------------

export function AddContactCard() {
  const router = useRouter();
  const [callConsent, setCallConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    const text = (key: string) => {
      const value = String(data.get(key) ?? "").trim();
      return value || undefined;
    };
    setBusy(true);
    setNote(null);
    const result = await addContact({
      name: text("name"),
      email: text("email"),
      phone: text("phone"),
      timezone: text("timezone"),
      goal: text("goal"),
      call_consent: callConsent,
      consent_date: callConsent ? text("consent_date") : undefined,
      consent_source: callConsent ? text("consent_source") : undefined,
      consent_attested: callConsent && data.get("attested") === "on",
      start_outreach: true,
    });
    setBusy(false);
    if (result.status === "error") return setNote({ ok: false, text: result.message });
    setNote({ ok: true, text: `Contact ${result.result.contact}; follow-up: ${humanize(result.result.plan)}.` });
    form.reset();
    setCallConsent(false);
    router.refresh();
  }

  return (
    <form
      className={card}
      onSubmit={(e) => {
        e.preventDefault();
        void submit(e.currentTarget);
      }}
    >
      <h2 className="text-[16px] font-semibold">Add one contact</h2>
      <div className="grid gap-2 sm:grid-cols-3">
        <Input name="name" placeholder="Name" maxLength={200} aria-label="Name" />
        <Input name="email" type="email" placeholder="Email" maxLength={320} aria-label="Email" />
        <Input name="phone" type="tel" placeholder="Phone" maxLength={40} aria-label="Phone" />
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <select name="timezone" aria-label="Their timezone" defaultValue="" className="h-9 rounded-[10px] border border-border bg-background px-2 text-[13px]">
          <option value="">Their timezone (needed for calls)</option>
          {US_ZONES.map(([zone, label]) => (
            <option key={zone} value={zone}>
              {label}
            </option>
          ))}
        </select>
        <Input name="goal" placeholder="Goal (optional)" maxLength={200} aria-label="Goal" />
      </div>
      <label className="flex items-center gap-2 text-[12.5px]">
        <input type="checkbox" className="size-4" checked={callConsent} onChange={(e) => setCallConsent(e.target.checked)} />
        They gave written consent to be called by our AI assistant
      </label>
      {callConsent ? (
        <div className="flex flex-col gap-2">
          <div className="grid gap-2 sm:grid-cols-2">
            <Input name="consent_date" type="date" required aria-label="Consent date" />
            <Input name="consent_source" required maxLength={300} placeholder="Where (e.g. signed web form)" aria-label="Consent source" />
          </div>
          <label className="flex items-start gap-2 text-[12.5px]">
            <input name="attested" type="checkbox" required className="mt-0.5 size-4" />
            I confirm this consent evidence is genuine.
          </label>
        </div>
      ) : null}
      <Button type="submit" size="sm" className="self-start" disabled={busy}>
        {busy ? "Adding…" : "Add and start follow-up"}
      </Button>
      <Note note={note} />
    </form>
  );
}

// -- Talk to Rebecca ----------------------------------------------------------------

export function RebeccaCard() {
  const router = useRouter();
  const textId = useId();
  const [text, setText] = useState("");
  const [draft, setDraft] = useState<CommandDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function ask() {
    setBusy(true);
    setNote(null);
    const result = await createCommand(text);
    setBusy(false);
    if (result.status === "error") return setNote({ ok: false, text: result.message });
    setDraft(result.draft);
  }

  async function decide(confirm: boolean) {
    if (!draft) return;
    setBusy(true);
    const result = confirm ? await confirmCommand(draft.draftId) : await discardCommand(draft.draftId);
    setBusy(false);
    if (result.status === "error") return setNote({ ok: false, text: result.message });
    setNote({
      ok: true,
      text: confirm && "result" in result.result ? `Done. ${counts(result.result.result)}` : "Cancelled — nothing was started.",
    });
    setDraft(null);
    setText("");
    router.refresh();
  }

  return (
    <div className={card}>
      <h2 className="text-[16px] font-semibold">Talk to Rebecca</h2>
      <label htmlFor={textId} className="text-[12.5px] text-muted-foreground">
        Describe who to follow up with, e.g. &quot;Email everyone who asked about roof repair last week&quot; or
        &quot;Stop following up with Jane Doe&quot;.
      </label>
      {!draft ? (
        <>
          <Textarea id={textId} rows={3} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} className="text-[13px]" />
          <Button type="button" size="sm" className="self-start" disabled={busy || text.trim().length < 3} onClick={() => void ask()}>
            {busy ? "Reading…" : "Ask Rebecca"}
          </Button>
        </>
      ) : (
        <div className="flex flex-col gap-2 rounded-[10px] bg-secondary px-4 py-3 text-[13px]">
          <p className="font-semibold">{draft.summary}</p>
          {draft.goal ? <p>Goal: {draft.goal}</p> : null}
          {draft.sample.length ? <p className="text-muted-foreground">Includes: {draft.sample.join(", ")}</p> : null}
          <div className="flex gap-2">
            <Button type="button" size="sm" disabled={busy || draft.contactCount === 0} onClick={() => void decide(true)}>
              Confirm
            </Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void decide(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      <Note note={note} />
    </div>
  );
}
