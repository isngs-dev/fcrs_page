"use client";

/**
 * AI voice confirmation call config -- a self-contained card (own state, own
 * save call), same pattern as `missed-call-config.tsx`. One question per line;
 * `{date}` and `{time}` are filled in with the booked slot.
 */
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { saveVoiceCallConfig, type VoiceCallConfigResult } from "@/lib/voice-calls";

export function VoiceCallConfigCard({
  result,
  tenantId,
}: {
  result: VoiceCallConfigResult;
  /** PLATFORM_ADMIN super-user save-path target -- `undefined` on the implicit route. */
  tenantId?: string;
}) {
  const questionsId = useId();
  const initial = result.status === "ok" ? result.config : null;
  const [enabled, setEnabled] = useState(initial?.enabled ?? false);
  const [questionsText, setQuestionsText] = useState((initial?.questions ?? []).join("\n"));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSaved(false);
    const questions = questionsText
      .split("\n")
      .map((q) => q.trim())
      .filter(Boolean);
    const saveResult = await saveVoiceCallConfig(enabled, questions, tenantId);
    setSaving(false);
    if (saveResult.status === "error") {
      setError(saveResult.message);
      return;
    }
    setQuestionsText(saveResult.config.questions.join("\n"));
    setSaved(true);
  }

  return (
    <div className="scroll-mt-16 rounded-[14px] border border-[var(--line)] bg-card px-[22px] pb-5 pt-1">
      <h2 className="pt-4 pb-0.5 text-[16px] font-semibold text-foreground">AI confirmation call</h2>
      <p className="pb-3 text-[12.5px] text-muted-foreground">
        About a minute after a visitor books a call through the chatbot, an automated voice call
        asks them these yes/no questions. Answers are saved on the lead. Visitors must give a phone
        number and agree to the call when booking. Requires Twilio to be set up on the server.
      </p>

      {result.status === "error" ? (
        <p role="alert" className="pb-3 text-[12.5px] text-destructive">
          Unable to load this setting. {result.message}
        </p>
      ) : null}

      <div className="flex flex-col gap-3">
        <label className="flex items-center gap-2 text-[12.5px] font-medium text-foreground">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="size-4"
          />
          Call visitors after they book
        </label>

        <div className="flex flex-col gap-1.5">
          <label htmlFor={questionsId} className="text-[12px] font-semibold text-foreground">
            Questions (one per line, up to 5)
          </label>
          <Textarea
            id={questionsId}
            value={questionsText}
            onChange={(e) => setQuestionsText(e.target.value)}
            rows={5}
            className="text-[13px]"
          />
          <p className="text-[11px] text-muted-foreground">
            Ask yes/no questions. Use {"{date}"} and {"{time}"} for the booked slot, e.g. &quot;Can
            you confirm you&apos;re available on {"{date}"} at {"{time}"}?&quot;
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
