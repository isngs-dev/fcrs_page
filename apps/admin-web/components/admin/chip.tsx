/**
 * Chip -- SR-15 D3/D4/D10/M11/M3. The design's `.chip` recipe
 * (Console.dc.html:37: 24px tall, 10px horizontal padding, 7px radius,
 * 11.5px/600 text, `--cream` background) with a `tone` prop for the
 * system's one semantic color pair (M3's success chip: `--success-bg`/
 * `--success-fg`, `--success-dot`).
 *
 * D4 (never color-only): `children` is required, so a success chip always
 * carries its own text label -- there is no variant that renders only a
 * colored dot. The optional dot is `aria-hidden` because the text already
 * carries the meaning; removing color (greyscale/color-blind) must not lose
 * information (WCAG 1.4.1).
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface ChipProps {
  tone?: "default" | "success";
  /** Show the success tone's decorative dot. Ignored for tone="default".
   * The dot is always `aria-hidden` -- it is never the only signal. */
  dot?: boolean;
  className?: string;
  children: ReactNode;
}

export function Chip({ tone = "default", dot = false, className, children }: ChipProps) {
  return (
    <span
      className={cn(
        "inline-flex h-6 items-center gap-1.5 rounded-[7px] px-2.5 text-[11.5px] font-semibold",
        tone === "success" ? "bg-[var(--success-bg)] text-[var(--success-fg)]" : "bg-secondary text-[var(--ink-2)]",
        className
      )}
    >
      {tone === "success" && dot ? (
        <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-[var(--success-dot)]" />
      ) : null}
      {children}
    </span>
  );
}
