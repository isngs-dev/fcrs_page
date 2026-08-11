"use client";

/**
 * Real round-robin auto-assignment toggle (SR-20 D1/D2), replacing SR-15's
 * inert, permanently-disabled placeholder. CLIENT_ADMIN-only (the page only
 * mounts this for a CLIENT_ADMIN; the copy below describes what the code
 * actually does).
 *
 * D2: copy says "new leads", NOT the mockup's "newly qualified" wording --
 * assignment happens at lead CREATION time (`stage='captured'`), not at
 * qualification. Shipping the mockup's literal copy over the real,
 * creation-time behavior would be the same defect class as a toggle that
 * does nothing (CLAUDE.md §3 / D2's rationale).
 */
import { useState, useTransition } from "react";
import { setAssignmentConfigAction } from "@/app/(protected)/members/assignment-actions";
import { SoftCard } from "@/components/admin/soft-card";

export function AssignmentToggle({ initialEnabled }: { initialEnabled: boolean }) {
  const [enabled, setEnabled] = useState(initialEnabled);
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function handleToggle() {
    const next = !enabled;
    setError(null);
    setEnabled(next); // optimistic UI flip; reverted below on failure
    startTransition(async () => {
      const result = await setAssignmentConfigAction(next);
      if (result.status === "error") {
        setEnabled(!next); // revert -- no silent fallback to a state the server didn't confirm
        setError(result.message ?? "Could not update auto-assignment.");
        return;
      }
      setEnabled(result.roundRobinEnabled ?? next);
    });
  }

  return (
    // Console.dc.html:360-369's Auto-assignment panel: `.tbl-card`-style card
    // (20px/22px padding, 15px/600 title), toggle track 40x24 with an 18px
    // knob (lines 363), title 13.5px/600 + description 12.5px muted.
    <SoftCard className="flex flex-col gap-3.5 px-[22px] py-5">
      <span className="text-[15px] font-semibold text-foreground">Auto-assignment</span>
      <div className="flex items-start gap-3.5">
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label="Round-robin new leads"
          disabled={isPending}
          onClick={handleToggle}
          className={`relative h-6 w-10 flex-none rounded-full transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60 ${
            enabled ? "bg-primary" : "bg-border"
          }`}
        >
          <span
            className={`absolute top-[3px] h-[18px] w-[18px] rounded-full bg-card transition-transform ${
              enabled ? "translate-x-4 left-[3px]" : "left-[3px]"
            }`}
          />
        </button>
        <div>
          <div className="text-[13.5px] font-semibold text-foreground">
            Round-robin new leads
          </div>
          <div className="mt-[3px] text-[12.5px] text-muted-foreground">
            Distribute new leads evenly across active agents as they arrive. Off by
            default — existing unassigned leads are never retroactively assigned.
          </div>
        </div>
      </div>
      {error ? (
        <p role="alert" className="text-[12px] text-destructive">
          {error}
        </p>
      ) : null}
    </SoftCard>
  );
}
