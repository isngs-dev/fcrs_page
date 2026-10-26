/**
 * Small presentational pieces shared by the Outreach pages (A8). No directive:
 * server components by default, also renderable inside client components.
 */
import Link from "next/link";
import { SoftCard } from "@/components/admin/soft-card";

export function OutreachHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="flex flex-col gap-1">
      <h1 className="text-[28px] font-semibold text-foreground">{title}</h1>
      <p className="text-[13px] text-muted-foreground">{subtitle}</p>
    </div>
  );
}

export function ErrorCard({ message, correlationId }: { message: string; correlationId?: string }) {
  return (
    <SoftCard role="alert" className="px-6 py-8 text-center">
      <p className="text-[14px] font-semibold text-foreground">{message}</p>
      {correlationId ? (
        <p className="mt-1 text-[11.5px] text-muted-foreground">Correlation ID: {correlationId}</p>
      ) : null}
    </SoftCard>
  );
}

export function EmptyCard({ title, body }: { title: string; body: string }) {
  return (
    <SoftCard role="status" className="px-6 py-12 text-center">
      <p className="text-[16px] font-semibold text-foreground">{title}</p>
      <p className="mx-auto mt-1 max-w-[420px] text-[13px] text-muted-foreground">{body}</p>
    </SoftCard>
  );
}

/** URL-driven filter pills, same chip recipe as Notifications. */
export function FilterPills({
  pills,
  active,
}: {
  pills: { label: string; href: string; value?: string }[];
  active?: string;
}) {
  return (
    <div role="group" aria-label="Filter" className="flex flex-wrap gap-[9px]">
      {pills.map((pill) => {
        const on = pill.value === active;
        return (
          <Link
            key={pill.label}
            href={pill.href}
            aria-current={on ? "true" : undefined}
            className="rounded-full px-3 py-[5px] text-[11.5px] font-semibold"
            style={
              on
                ? { background: "#333333", color: "#fbfaf7" }
                : { background: "#fff", border: "1px solid var(--line)", color: "var(--ink-2)" }
            }
          >
            {pill.label}
          </Link>
        );
      })}
    </div>
  );
}
