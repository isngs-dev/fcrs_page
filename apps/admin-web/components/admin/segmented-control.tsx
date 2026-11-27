/**
 * Shared `.seg` segmented-control primitive (SR-27 slice 0, decision D14).
 *
 * Exact recipe from `Console.dc.html:46-48`:
 *   .seg{display:flex;background:var(--cream);border-radius:10px;padding:3px}
 *   .seg > *{height:32px;padding:0 16px;display:flex;align-items:center;
 *     font-size:13px;font-weight:600;border-radius:8px;color:var(--muted);cursor:pointer}
 *   .seg > .on{background:var(--near-black);color:#fbfaf7}
 *
 * This is the THIRD+ place this exact recipe was needed (Leads Table/Board
 * toggle, Leads sort/filter chips, Team members, now Conversations status
 * filter) -- SR-27 extracts it once so it stops being re-derived per page.
 * Server-renderable, Link-based (URL is the only state), `aria-current` on
 * the active item -- never client-side `useState`, matching this app's
 * server-first URL-state model (leads-view-toggle.tsx's existing pattern,
 * which this component generalizes and which that file now delegates to).
 */
import Link from "next/link";

export interface SegmentedControlItem {
  /** Stable key for React + used as the item's visible label unless `label` is given. */
  key: string;
  label: string;
  href: string;
  active: boolean;
}

export function SegmentedControl({
  items,
  ariaLabel,
  scroll = false,
}: {
  items: SegmentedControlItem[];
  ariaLabel: string;
  scroll?: boolean;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="flex items-center gap-0.5 rounded-[10px] border border-border bg-secondary p-[3px] text-[13px] font-semibold"
    >
      {items.map((item) => (
        <Link
          key={item.key}
          href={item.href}
          scroll={scroll}
          aria-current={item.active ? "true" : undefined}
          className="flex h-[30px] items-center rounded-[8px] px-3.5 transition-colors duration-150 hover:text-foreground"
          style={
            item.active
              ? { background: "var(--card)", color: "var(--primary)", boxShadow: "var(--shadow-card)" }
              : { color: "var(--muted-foreground)" }
          }
        >
          {item.label}
        </Link>
      ))}
    </div>
  );
}
