import Image from "next/image";
import { cn } from "@/lib/utils";

/**
 * The iSN logo (owner, 2026-11-27). Its navy wordmark disappears on the pine sidebar,
 * so on dark surfaces it sits on a white tile (`tile`); on light surfaces it is used bare.
 * Source: public/isn-logo.png (242x87, transparent).
 */
export function BrandLogo({
  height = 20,
  tile = false,
  priority = false,
  className,
}: {
  height?: number;
  tile?: boolean;
  priority?: boolean;
  className?: string;
}) {
  const image = (
    <Image
      src="/isn-logo.png"
      alt="iSN"
      width={242}
      height={87}
      priority={priority}
      style={{ height, width: "auto" }}
    />
  );
  if (!tile) return <span className={cn("inline-flex shrink-0", className)}>{image}</span>;
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-[9px] bg-white px-2 py-[7px] shadow-[0_1px_2px_rgba(0,0,0,0.18)]",
        className
      )}
    >
      {image}
    </span>
  );
}
