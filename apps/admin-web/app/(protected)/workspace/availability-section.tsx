"use client";

/**
 * Scheduling-availability settings section (Tier 2, sales-call-booking flow
 * fix). Lets a CLIENT_ADMIN set the tenant's native-booking availability --
 * timezone, weekly hours per day, slot length, buffer -- via
 * `PUT /admin/schedule/availability` (`availability-actions.ts`). Mirrors
 * `workspace-section.tsx`'s "own SoftCard, own `useActionState`, own PUT
 * endpoint" pattern (a distinct backend resource from the qualitative bot
 * settings `SettingsForm` saves), with the day-toggle grid UI lifted from
 * `business-hours-picker.tsx` (seven day-squares + open/close time fields
 * per active day) since both edit the same "day -> [open, close]" shape.
 *
 * IMPORTANT LIMITATION (documented, not hidden): there is no
 * `GET /admin/schedule/availability` endpoint today, so this form cannot be
 * pre-populated with the tenant's CURRENT availability the way
 * `SettingsForm`/`WorkspaceSection` are -- it always starts from
 * `DEFAULT_WEEKLY_HOURS` (Mon-Fri 09:00-18:00 UTC, the same default this
 * platform seeds), and after a successful save shows the PUT response's
 * confirmed values (same "confirmed, not optimistic" decision as
 * `settings/actions.ts`) until the page is reloaded, at which point it
 * resets back to the default rather than reflecting whatever was last
 * saved. Adding a GET endpoint to fix that is a natural follow-up, out of
 * scope for this pass (the plan this session implements calls only for
 * wiring the EXISTING `PUT` endpoint).
 */
import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { SoftCard } from "@/components/admin/soft-card";
import { SetRow, SET_ROW_FIELD_CLASS } from "@/components/admin/set-row";
import { saveAvailability, type SaveAvailabilityState } from "@/app/(protected)/workspace/availability-actions";
import {
  DAY_KEYS,
  DAY_LABELS,
  DEFAULT_WEEKLY_HOURS,
  serializeWeeklyHours,
  type DayKey,
  type WeeklyHoursState,
} from "@/lib/availability-schema";

const initialState: SaveAvailabilityState = { status: "idle" };

function SaveButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" disabled={pending}>
      {pending ? "Saving…" : "Save availability"}
    </Button>
  );
}

export function AvailabilitySection() {
  const [state, formAction] = useActionState(saveAvailability, initialState);

  const [timezone, setTimezone] = useState("UTC");
  const [slotMinutes, setSlotMinutes] = useState("30");
  const [bufferMinutes, setBufferMinutes] = useState("0");
  const [weeklyHours, setWeeklyHours] = useState<WeeklyHoursState>(DEFAULT_WEEKLY_HOURS);

  const fieldErrors = state.status === "error" ? state.fieldErrors : {};
  const formError = state.status === "error" ? state.formError : null;

  function toggleDay(day: DayKey) {
    setWeeklyHours((prev) => ({
      ...prev,
      [day]: prev[day] ? null : ["09:00", "18:00"],
    }));
  }

  function setDayTime(day: DayKey, index: 0 | 1, time: string) {
    setWeeklyHours((prev) => {
      const current = prev[day];
      if (!current) return prev;
      const next: [string, string] = [...current] as [string, string];
      next[index] = time;
      return { ...prev, [day]: next };
    });
  }

  const activeDayCount = DAY_KEYS.filter((day) => weeklyHours[day] !== null).length;

  return (
    <SoftCard className="flex scroll-mt-16 flex-col px-[22px] pb-2 pt-1" id="settings-availability">
      <h2 className="pb-0.5 pt-4 text-[15px] font-semibold text-foreground">
        Scheduling availability
      </h2>
      <p className="pb-2 text-xs text-muted-foreground">
        Controls the calendar the &quot;Connect with a sales rep&quot; flow offers visitors.
      </p>

      {state.status === "saved" ? (
        <p role="status" className="pt-2 text-xs text-muted-foreground">
          Saved.
        </p>
      ) : null}
      {formError ? (
        <p role="alert" className="mt-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <form action={formAction}>
        <SetRow
          label="Time zone"
          description='An IANA time zone identifier, e.g. "UTC" or "America/New_York".'
          htmlFor="availability-timezone"
        >
          <input
            id="availability-timezone"
            name="timezone"
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            placeholder="UTC"
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.timezone ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.timezone}
            </p>
          ) : null}
        </SetRow>

        <SetRow
          label="Weekly hours"
          description="Toggle the days sales calls can be booked, and set each day's open/close time."
          htmlFor="availability-day-mon"
        >
          <div className="flex flex-col gap-2.5">
            <div className="flex gap-1.5">
              {DAY_KEYS.map((day) => {
                const active = weeklyHours[day] !== null;
                return (
                  <button
                    key={day}
                    type="button"
                    id={day === "mon" ? "availability-day-mon" : undefined}
                    onClick={() => toggleDay(day)}
                    aria-pressed={active}
                    aria-label={`${DAY_LABELS[day]}${active ? ", open" : ", closed"}`}
                    className="grid size-8 place-items-center rounded-lg text-[12px] font-semibold transition-colors"
                    style={
                      active
                        ? { background: "#333333", color: "#fbfaf7" }
                        : { background: "var(--secondary)", color: "var(--muted-foreground)" }
                    }
                  >
                    {DAY_LABELS[day][0]}
                  </button>
                );
              })}
            </div>

            <div className="flex flex-col gap-2">
              {DAY_KEYS.filter((day) => weeklyHours[day] !== null).map((day) => {
                const window = weeklyHours[day]!;
                return (
                  <div key={day} className="flex items-center gap-2 text-[12px]">
                    <span className="w-8 shrink-0 font-semibold text-foreground">
                      {DAY_LABELS[day]}
                    </span>
                    <label className="sr-only" htmlFor={`availability-${day}-open`}>
                      {DAY_LABELS[day]} open time
                    </label>
                    <input
                      id={`availability-${day}-open`}
                      type="time"
                      value={window[0]}
                      onChange={(e) => setDayTime(day, 0, e.target.value)}
                      className="rounded-[8px] border border-[var(--line)] px-2 py-1 text-[12px]"
                    />
                    <span className="text-muted-foreground">–</span>
                    <label className="sr-only" htmlFor={`availability-${day}-close`}>
                      {DAY_LABELS[day]} close time
                    </label>
                    <input
                      id={`availability-${day}-close`}
                      type="time"
                      value={window[1]}
                      onChange={(e) => setDayTime(day, 1, e.target.value)}
                      className="rounded-[8px] border border-[var(--line)] px-2 py-1 text-[12px]"
                    />
                  </div>
                );
              })}
              {activeDayCount === 0 ? (
                <p className="text-[11px] text-muted-foreground">
                  No days open. Click a day above to set its hours.
                </p>
              ) : null}
            </div>
          </div>
          {fieldErrors.weeklyHoursJson ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.weeklyHoursJson}
            </p>
          ) : null}
          <input type="hidden" name="weeklyHoursJson" value={serializeWeeklyHours(weeklyHours)} readOnly />
        </SetRow>

        <SetRow label="Slot length" description="Meeting length, in minutes." htmlFor="availability-slot-minutes">
          <input
            id="availability-slot-minutes"
            name="slotMinutes"
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            value={slotMinutes}
            onChange={(e) => setSlotMinutes(e.target.value)}
            className={`${SET_ROW_FIELD_CLASS} w-24`}
          />
          {fieldErrors.slotMinutes ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.slotMinutes}
            </p>
          ) : null}
        </SetRow>

        <SetRow
          label="Buffer"
          description="Minutes of gap kept free before/after each booked slot."
          htmlFor="availability-buffer-minutes"
          isLast
        >
          <input
            id="availability-buffer-minutes"
            name="bufferMinutes"
            type="number"
            min={0}
            step={1}
            inputMode="numeric"
            value={bufferMinutes}
            onChange={(e) => setBufferMinutes(e.target.value)}
            className={`${SET_ROW_FIELD_CLASS} w-24`}
          />
          {fieldErrors.bufferMinutes ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.bufferMinutes}
            </p>
          ) : null}
        </SetRow>

        <div className="py-3">
          <SaveButton />
        </div>
      </form>
    </SoftCard>
  );
}
