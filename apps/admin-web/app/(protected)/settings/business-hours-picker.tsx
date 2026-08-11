"use client";

/**
 * Business-hours day-picker popover (SR-27 slice 8, `Console.dc.html:528`:
 * a 320px panel, seven 32x32 day squares, Open/Close time fields).
 *
 * Backend reality (`lib/settings-schema.ts:56-60`): `business_hours` is an
 * ARBITRARY JSON object with no enforced schema -- the server accepts
 * whatever the textarea contains as long as it parses as a JSON object (or
 * is blank). The reference's day-picker implies a `{day: [open, close]}`
 * shape, which IS a real, expressible subset of that arbitrary-object
 * contract -- so this component edits exactly that shape and serializes it
 * back into the same `businessHoursText` string the existing textarea/PUT
 * payload already uses. It does not add any control the backend can't
 * accept; it constrains editing to a shape the backend already tolerates.
 * Toggling a day off removes its key entirely (never writes an empty/null
 * entry); the raw-JSON textarea stays present alongside this popover as the
 * escape hatch for any shape this UI can't express (e.g. per-day multiple
 * ranges) -- editing either one keeps the other in sync via the same
 * `businessHoursText` string.
 */
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";

const DAYS = [
  { key: "mon", label: "M" },
  { key: "tue", label: "T" },
  { key: "wed", label: "W" },
  { key: "thu", label: "T" },
  { key: "fri", label: "F" },
  { key: "sat", label: "S" },
  { key: "sun", label: "S" },
] as const;

type DayHours = Record<string, [string, string]>;

function parseDayHours(text: string): DayHours {
  if (!text.trim()) return {};
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const result: DayHours = {};
      for (const [day, value] of Object.entries(parsed as Record<string, unknown>)) {
        if (Array.isArray(value) && value.length === 2 && typeof value[0] === "string" && typeof value[1] === "string") {
          result[day] = [value[0], value[1]];
        }
      }
      return result;
    }
  } catch {
    // Not valid JSON (or not this shape) -- the popover starts empty; the
    // raw textarea remains the source of truth for whatever it holds.
  }
  return {};
}

function serializeDayHours(hours: DayHours): string {
  if (Object.keys(hours).length === 0) return "";
  return JSON.stringify(hours, null, 2);
}

export function BusinessHoursPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (nextText: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const popoverId = useId();
  const hours = parseDayHours(value);

  function toggleDay(day: string) {
    const next = { ...hours };
    if (next[day]) {
      delete next[day];
    } else {
      next[day] = ["09:00", "17:00"];
    }
    onChange(serializeDayHours(next));
  }

  function setDayTime(day: string, index: 0 | 1, time: string) {
    if (!hours[day]) return;
    const next = { ...hours };
    const pair: [string, string] = [...next[day]] as [string, string];
    pair[index] = time;
    next[day] = pair;
    onChange(serializeDayHours(next));
  }

  const activeDayCount = Object.keys(hours).length;

  return (
    <div className="relative inline-block">
      <Button type="button" variant="outline" size="sm" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-controls={popoverId}>
        {activeDayCount > 0 ? `${activeDayCount} day${activeDayCount === 1 ? "" : "s"} set` : "Set business hours"}
      </Button>

      {open ? (
        <div
          id={popoverId}
          role="dialog"
          aria-label="Business hours"
          className="absolute left-0 top-[calc(100%+8px)] z-20 w-[320px] rounded-[12px] border border-[var(--line)] bg-card p-4"
          style={{ boxShadow: "0 16px 44px rgba(0,0,0,.16)" }}
        >
          <div className="flex justify-between gap-1.5">
            {DAYS.map((day) => {
              const active = Boolean(hours[day.key]);
              return (
                <button
                  key={day.key}
                  type="button"
                  onClick={() => toggleDay(day.key)}
                  aria-pressed={active}
                  aria-label={day.key}
                  className="grid size-8 place-items-center rounded-lg text-[12px] font-semibold transition-colors"
                  style={
                    active
                      ? { background: "#333333", color: "#fbfaf7" }
                      : { background: "var(--secondary)", color: "var(--muted-foreground)" }
                  }
                >
                  {day.label}
                </button>
              );
            })}
          </div>

          <div className="mt-3 flex flex-col gap-2">
            {DAYS.filter((day) => hours[day.key]).map((day) => (
              <div key={day.key} className="flex items-center gap-2 text-[12px]">
                <span className="w-8 shrink-0 font-semibold text-foreground">{day.key}</span>
                <label className="sr-only" htmlFor={`${popoverId}-${day.key}-open`}>
                  {day.key} open time
                </label>
                <input
                  id={`${popoverId}-${day.key}-open`}
                  type="time"
                  value={hours[day.key][0]}
                  onChange={(e) => setDayTime(day.key, 0, e.target.value)}
                  className="rounded-[8px] border border-[var(--line)] px-2 py-1 text-[12px]"
                />
                <span className="text-muted-foreground">–</span>
                <label className="sr-only" htmlFor={`${popoverId}-${day.key}-close`}>
                  {day.key} close time
                </label>
                <input
                  id={`${popoverId}-${day.key}-close`}
                  type="time"
                  value={hours[day.key][1]}
                  onChange={(e) => setDayTime(day.key, 1, e.target.value)}
                  className="rounded-[8px] border border-[var(--line)] px-2 py-1 text-[12px]"
                />
              </div>
            ))}
            {activeDayCount === 0 ? (
              <p className="text-[11px] text-muted-foreground">
                No days set. Click a day above to add open/close hours.
              </p>
            ) : null}
          </div>

          <div className="mt-3 flex justify-end">
            <Button type="button" size="sm" onClick={() => setOpen(false)}>
              Done
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
