/**
 * TableCard / TableHeadCell / TableCell / TableRow -- SR-15 D3/D10/M11. The
 * design's `.tbl-card`/`.th`/`.th-cell`/`.td`/`.row` recipe
 * (Console.dc.html:25-35): white card, 1px `--line` border, 12px radius,
 * `overflow-hidden`; header row `--cream` background with an 11px/600
 * uppercase `.06em`-tracked `--muted-foreground` label; body cells 13px with
 * a 56px min height and `--row-line` dividers (deliberately lighter than
 * the card's own `--line` border, per D1's rationale).
 *
 * These wrap a plain `<table>`, not `components/ui/table.tsx`'s shadcn
 * primitive -- the 12px radius and the `--row-line`/`--line` distinction
 * fall outside what that primitive (and the `--radius` multiplier ramp)
 * expresses (D3's rejected-alternatives). `TableHeadCell` always emits
 * `scope="col"` so every restyled table stays screen-reader-correct without
 * each call site re-adding it by hand.
 */
import type { ReactNode, ThHTMLAttributes, TdHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export function TableCard({
  children,
  className,
  footer,
  allowOverflow = false,
}: {
  children: ReactNode;
  className?: string;
  /** SR-24 item 17: optional footer row (e.g. pagination) rendered as a
   * sibling of the `<table>`, inside the same bordered card -- not a
   * standalone card of its own. Omitted entirely when not passed, so every
   * other `TableCard` consumer is unaffected. */
  footer?: ReactNode;
  /** Lets a table-owned popover escape the card without changing other tables. */
  allowOverflow?: boolean;
}) {
  return (
    <div
      className={cn(
        allowOverflow ? "overflow-visible" : "overflow-hidden",
        "rounded-[12px] border border-border bg-card",
        className
      )}
    >
      <table className="w-full border-collapse text-[13px]">{children}</table>
      {footer ? (
        <div className={allowOverflow ? "overflow-hidden rounded-b-[11px]" : undefined}>{footer}</div>
      ) : null}
    </div>
  );
}

export function TableHeadCell({
  className,
  children,
  leftIcon,
  rightControls,
  ...props
}: ThHTMLAttributes<HTMLTableCellElement> & {
  /** Optional decorative, `aria-hidden` leading icon for the header label. */
  leftIcon?: ReactNode;
  /** Optional header-local sort/filter controls (SR-25); the semantic
   * column state stays on this <th> via its ordinary aria-sort prop. */
  rightControls?: ReactNode;
}) {
  return (
    <th
      scope="col"
      className={cn(
        "border-r border-[var(--row-line)] bg-secondary px-3.5 py-2.5 text-left text-[11px] font-semibold tracking-[.06em] text-muted-foreground uppercase first:rounded-tl-[11px] last:rounded-tr-[11px] last:border-r-0",
        className
      )}
      {...props}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {leftIcon ? (
            <span aria-hidden className="inline-flex shrink-0 items-center">
              {leftIcon}
            </span>
          ) : null}
          {children}
        </span>
        {rightControls ? <span className="ml-auto flex shrink-0 items-center gap-0.5">{rightControls}</span> : null}
      </span>
    </th>
  );
}

export function TableCell({ className, children, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td
      className={cn("min-h-14 border-t border-[var(--row-line)] px-3.5 py-3 align-middle", className)}
      {...props}
    >
      {children}
    </td>
  );
}

/** Row wrapper carrying the design's row hover treatment
 * (Console.dc.html's `.row:hover { background:#faf9f6 }`, M11). Plain
 * `<tr>` pass-through with `className` merge so call sites can still add a
 * selected/highlighted background per-row. */
export function TableRow({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr className={cn("hover:bg-[#faf9f6]", className)} {...props}>
      {children}
    </tr>
  );
}
