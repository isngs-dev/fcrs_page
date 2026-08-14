/**
 * Shared `.setnav` left-rail primitive (SR-27 slice 0), used by both the
 * Settings and Bot-settings shells (`Console.dc.html:869-876,512-518`):
 *
 *   .setnav{display:flex;align-items:center;gap:9px;padding:8px 11px;
 *          border-radius:8px;font-size:13px;color:var(--ink-2);
 *          text-decoration:none;cursor:pointer}
 *   .setnav:hover{background:#e6e6e6}
 *   .setnav.on{background:var(--cream);color:var(--ink);font-weight:600}
 *
 * Rail geometry: 184px width, `border-right:1px solid var(--line)`,
 * `padding:20px 14px`, `gap:2px` between rows, an uppercase `10.5px/.1em`
 * eyebrow label above the rows.
 *
 * Three row variants:
 *  - link: a real in-app route (`href` navigates elsewhere, e.g. Members).
 *  - anchor: an in-page `#hash` link within the same shell (e.g. API keys).
 *  - disabled: no backing capability (e.g. Billing, Appearance) -- rendered
 *    `aria-disabled`, no `href`, muted, never a dead clickable link.
 */
import Link from "next/link";

export type SettingsRailRow =
  | { kind: "link"; key: string; label: string; href: string; icon?: React.ReactNode; active?: boolean }
  | { kind: "anchor"; key: string; label: string; href: string; icon?: React.ReactNode; active?: boolean }
  | { kind: "disabled"; key: string; label: string; icon?: React.ReactNode };

function rowClass(active: boolean | undefined) {
  return `flex items-center gap-[9px] rounded-lg px-[11px] py-2 text-[13px] transition-colors ${
    active ? "bg-secondary font-semibold text-foreground" : "text-[var(--ink-2)] hover:bg-[#e6e6e6]"
  }`;
}

export function SettingsRail({ eyebrow, rows }: { eyebrow: string; rows: readonly SettingsRailRow[] }) {
  return (
    <nav
      aria-label={eyebrow}
      className="flex w-[184px] flex-none flex-col gap-0.5 border-r border-[var(--line)] px-[14px] py-5"
    >
      <p className="px-[11px] pb-2 text-[10.5px] font-semibold uppercase tracking-[.1em] text-muted-foreground">
        {eyebrow}
      </p>
      {rows.map((row) => {
        if (row.kind === "disabled") {
          return (
            <span
              key={row.key}
              aria-disabled="true"
              className="flex cursor-not-allowed items-center gap-[9px] rounded-lg px-[11px] py-2 text-[13px] text-muted-foreground/60"
            >
              {row.icon}
              {row.label}
            </span>
          );
        }
        return (
          <Link key={row.key} href={row.href} className={rowClass(row.active)}>
            {row.icon}
            {row.label}
          </Link>
        );
      })}
    </nav>
  );
}
