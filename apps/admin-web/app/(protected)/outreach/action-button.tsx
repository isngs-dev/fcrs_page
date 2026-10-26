"use client";

/**
 * One button for every one-shot Outreach mutation (approve, discard, stop,
 * pause, take over, remove): optional confirm, call the bound server action,
 * show its error, refresh the server-rendered page (or navigate away).
 */
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";

export function ActionButton({
  action,
  label,
  pendingLabel,
  confirmText,
  variant = "default",
  doneHref,
}: {
  action: () => Promise<{ status: "ok" } | { status: "error"; message: string }>;
  label: string;
  pendingLabel?: string;
  confirmText?: string;
  variant?: "default" | "outline" | "secondary" | "destructive" | "ghost";
  /** Navigate here after success instead of refreshing in place. */
  doneHref?: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    setError(null);
    const result = await action();
    setBusy(false);
    if (result.status === "error") {
      setError(result.message);
      return;
    }
    if (doneHref) router.push(doneHref);
    else router.refresh();
  }

  return (
    <span className="inline-flex flex-col gap-1">
      <Button type="button" size="sm" variant={variant} disabled={busy} onClick={() => void run()}>
        {busy ? (pendingLabel ?? "Working…") : label}
      </Button>
      {error ? (
        <span role="alert" className="max-w-[320px] text-[12px] text-destructive">
          {error}
        </span>
      ) : null}
    </span>
  );
}
