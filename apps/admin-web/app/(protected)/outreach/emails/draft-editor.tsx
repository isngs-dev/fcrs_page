"use client";

/** Edit a draft's subject/body (A8 U4) -- saved via `PUT /emails/{id}`. */
import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { saveDraft } from "@/lib/outreach";

export function DraftEditor({ messageId, subject, bodyText }: { messageId: string; subject: string; bodyText: string }) {
  const router = useRouter();
  const subjectId = useId();
  const bodyId = useId();
  const [draftSubject, setSubject] = useState(subject);
  const [draftBody, setBody] = useState(bodyText);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const changed = draftSubject !== subject || draftBody !== bodyText;

  async function save() {
    setBusy(true);
    setNote(null);
    const result = await saveDraft(messageId, draftSubject, draftBody);
    setBusy(false);
    if (result.status === "error") {
      setNote({ ok: false, text: result.message });
      return;
    }
    setNote({ ok: true, text: "Saved." });
    router.refresh();
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <label htmlFor={subjectId} className="text-[12px] font-semibold">
          Subject
        </label>
        <Input id={subjectId} value={draftSubject} maxLength={150} onChange={(e) => setSubject(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={bodyId} className="text-[12px] font-semibold">
          Message
        </label>
        <Textarea id={bodyId} value={draftBody} rows={12} onChange={(e) => setBody(e.target.value)} className="text-[13px]" />
      </div>
      {note ? (
        <p role={note.ok ? "status" : "alert"} className={note.ok ? "text-[12.5px] text-[#3f7d57]" : "text-[12.5px] text-destructive"}>
          {note.text}
        </p>
      ) : null}
      <Button type="button" size="sm" variant="outline" className="self-start" disabled={busy || !changed} onClick={() => void save()}>
        {busy ? "Saving…" : "Save changes"}
      </Button>
    </div>
  );
}
