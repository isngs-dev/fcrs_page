"use client";

/**
 * A campaign's end date (A12 F5): no new outreach after the end of that day (the
 * admin's local midnight). Blank + Save clears it.
 */
import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { setCampaignEnd } from "@/lib/outreach";

/** "2026-12-31" -> the end of that day in the browser's timezone, as an ISO instant. */
export function endOfDayIso(day: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  return new Date(`${day}T23:59:00`).toISOString();
}

export function CampaignEndDate({ campaignId, endsAt }: { campaignId: string; endsAt: string | null }) {
  const router = useRouter();
  const inputId = useId();
  const initial = endsAt ? new Date(endsAt).toLocaleDateString("en-CA") : "";
  const [day, setDay] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    const result = await setCampaignEnd(campaignId, day ? endOfDayIso(day) : null);
    setBusy(false);
    if (result.status === "error") return setError(result.message);
    router.refresh();
  }

  return (
    <span className="inline-flex flex-col gap-1">
      <span className="inline-flex items-center gap-1.5">
        <label htmlFor={inputId} className="sr-only">
          Campaign end date
        </label>
        <input
          id={inputId}
          type="date"
          value={day}
          onChange={(e) => setDay(e.target.value)}
          className="h-8 rounded-[8px] border border-border bg-background px-2 text-[12.5px]"
        />
        <Button type="button" size="sm" variant="outline" disabled={busy || day === initial} onClick={() => void save()}>
          {busy ? "…" : "Save"}
        </Button>
      </span>
      {error ? (
        <span role="alert" className="text-[12px] text-destructive">
          {error}
        </span>
      ) : null}
    </span>
  );
}
