/**
 * SoftCard -- SR-15 D3/D10/M11. The design's `.soft-card` recipe
 * (Console.dc.html:64: `background:var(--card); border:1px solid
 * var(--line); border-radius:14px; box-shadow:0 1px 2px rgba(28,27,25,.03)`)
 * as a React component with `className` pass-through, per D3's rejection of
 * a parallel global CSS class. Presentational only -- no "use client", no
 * data fetching, no tenant-scoped prop -- so every server component that
 * imports it stays a server component (admin-web skill: server-first).
 *
 * Radius is a literal Tailwind arbitrary value (D10), not derived from the
 * shadcn `--radius` multiplier ramp: 14px doesn't fall out of that ramp's
 * factors, and forcing it through `--radius` would re-shape every other
 * shadcn primitive in the tree that also consumes it.
 */
import type { ComponentPropsWithoutRef, ElementType } from "react";
import { cn } from "@/lib/utils";

export function SoftCard<T extends ElementType = "div">({
  as,
  className,
  ...props
}: { as?: T; className?: string } & Omit<ComponentPropsWithoutRef<T>, "as" | "className">) {
  const Component = as ?? "div";
  return (
    <Component
      className={cn(
        "rounded-[14px] border border-border bg-card shadow-[0_1px_2px_rgba(28,27,25,.03)]",
        className
      )}
      {...props}
    />
  );
}
