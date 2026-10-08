"use client";

/**
 * AI voice agent ("Call Us") config -- a self-contained card (own state, own
 * save call), same pattern as `voice-call-config.tsx`.
 */
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { saveVoiceAgentConfig, type VoiceAgentConfigResult } from "@/lib/voice-agent";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function VoiceAgentConfigCard({
  result,
  tenantId,
}: {
  result: VoiceAgentConfigResult;
  /** PLATFORM_ADMIN super-user save-path target -- `undefined` on the implicit route. */
  tenantId?: string;
}) {
  const ids = { greeting: useId(), number: useId(), zone: useId(), open: useId(), close: useId(), limit: useId() };
  const initial = result.status === "ok" ? result.config : null;
  const [enabled, setEnabled] = useState(initial?.enabled ?? false);
  const [greeting, setGreeting] = useState(initial?.greeting ?? "");
  const [transferNumber, setTransferNumber] = useState(initial?.transferNumber ?? "+17868233553");
  const [timezone, setTimezone] = useState(initial?.timezone ?? "America/New_York");
  const [openTime, setOpenTime] = useState(initial?.openTime ?? "09:00");
  const [closeTime, setCloseTime] = useState(initial?.closeTime ?? "18:00");
  const [openDays, setOpenDays] = useState<number[]>(initial?.openDays ?? [0, 1, 2, 3, 4]);
  const [maxMinutes, setMaxMinutes] = useState(String(initial?.maxMinutes ?? 10));
  const [plivoReady, setPlivoReady] = useState(initial?.plivoReady ?? false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function toggleDay(day: number) {
    setOpenDays((days) => (days.includes(day) ? days.filter((d) => d !== day) : [...days, day].sort()));
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSaved(false);
    const saveResult = await saveVoiceAgentConfig(
      {
        enabled,
        greeting: greeting.trim() || null,
        transferNumber,
        timezone,
        openTime,
        closeTime,
        openDays,
        maxMinutes: Number(maxMinutes) || 10,
      },
      tenantId
    );
    setSaving(false);
    if (saveResult.status === "error") {
      setError(saveResult.message);
      return;
    }
    setTransferNumber(saveResult.config.transferNumber);
    setPlivoReady(saveResult.config.plivoReady);
    setSaved(true);
  }

  const label = "text-[12px] font-semibold text-foreground";

  return (
    <div className="scroll-mt-16 rounded-[14px] border border-[var(--line)] bg-card px-[22px] pb-5 pt-1">
      <h2 className="pt-4 pb-0.5 text-[16px] font-semibold text-foreground">AI voice agent (Call Us)</h2>
      <p className="pb-3 text-[12.5px] text-muted-foreground">
        Adds a Call Us button to the chatbot. Visitors talk to an AI agent in their browser; it
        answers from your knowledge base and transfers the call to your team when it can&apos;t help
        or the caller asks for a person. Every call&apos;s transcript is saved in Conversations.
      </p>

      {result.status === "error" ? (
        <p role="alert" className="pb-3 text-[12.5px] text-destructive">
          Unable to load this setting. {result.message}
        </p>
      ) : null}
      {!plivoReady ? (
        <p className="mb-3 rounded-md bg-[#fff9ec] px-3 py-2 text-[12px] text-[#6a4e00]">
          Plivo browser calling isn&apos;t set up on the server yet, so Call Us stays hidden even
          when switched on.
        </p>
      ) : null}

      <div className="flex flex-col gap-3">
        <label className="flex items-center gap-2 text-[12.5px] font-medium text-foreground">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="size-4" />
          Show Call Us in the chatbot
        </label>

        <div className="flex flex-col gap-1.5">
          <label htmlFor={ids.greeting} className={label}>Greeting (leave blank for the default)</label>
          <Textarea
            id={ids.greeting}
            value={greeting}
            onChange={(e) => setGreeting(e.target.value)}
            rows={2}
            maxLength={400}
            placeholder="Hi, thanks for calling. I'm an AI assistant and I can answer questions about our services. How can I help you today?"
            className="text-[13px]"
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor={ids.number} className={label}>Transfer calls to</label>
          <Input id={ids.number} value={transferNumber} onChange={(e) => setTransferNumber(e.target.value)} className="max-w-[220px] text-[13px]" />
        </div>

        <fieldset className="flex flex-col gap-1.5">
          <legend className={label}>Team hours (transfers only ring during these hours)</legend>
          <div className="flex flex-wrap gap-1.5 pt-1">
            {DAYS.map((day, index) => (
              <label key={day} className="flex items-center gap-1 rounded-md border border-[var(--line)] px-2 py-1 text-[12px]">
                <input type="checkbox" checked={openDays.includes(index)} onChange={() => toggleDay(index)} className="size-3.5" />
                {day}
              </label>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2 pt-1 text-[12px]">
            <label htmlFor={ids.open} className="sr-only">Opens at</label>
            <Input id={ids.open} type="time" value={openTime} onChange={(e) => setOpenTime(e.target.value)} className="w-[120px] text-[13px]" />
            <span>to</span>
            <label htmlFor={ids.close} className="sr-only">Closes at</label>
            <Input id={ids.close} type="time" value={closeTime} onChange={(e) => setCloseTime(e.target.value)} className="w-[120px] text-[13px]" />
            <label htmlFor={ids.zone} className="sr-only">Time zone</label>
            <Input id={ids.zone} value={timezone} onChange={(e) => setTimezone(e.target.value)} className="w-[190px] text-[13px]" />
          </div>
          <p className="text-[11px] text-muted-foreground">
            Outside these hours the agent offers Schedule a Call instead of transferring. Time zone
            as a name like America/New_York.
          </p>
        </fieldset>

        <div className="flex flex-col gap-1.5">
          <label htmlFor={ids.limit} className={label}>Maximum call length (minutes)</label>
          <Input
            id={ids.limit}
            type="number"
            min={1}
            max={60}
            value={maxMinutes}
            onChange={(e) => setMaxMinutes(e.target.value)}
            className="w-[100px] text-[13px]"
          />
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
