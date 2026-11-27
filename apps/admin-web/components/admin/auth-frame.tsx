import type { ReactNode } from "react";
import { BrandLogo } from "@/components/admin/brand-logo";

/**
 * Pine & Clay (owner, 2026-11-27): the sign-in, forgot-password and reset-password pages
 * share one split frame -- a pine brand panel (hidden on small screens) beside the form.
 */
export function AuthFrame({
  title,
  description,
  children,
}: {
  title: string;
  description: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-screen flex-1">
      <aside className="relative hidden w-[44%] max-w-[560px] flex-col justify-between overflow-hidden bg-sidebar p-10 text-sidebar-foreground lg:flex">
        <div className="flex items-center gap-3">
          <BrandLogo tile priority height={22} />
          <span className="text-[15px] font-semibold text-white">ChatLeads</span>
        </div>
        <div className="relative z-10 max-w-sm">
          <p className="text-[28px] leading-tight font-semibold tracking-[-0.015em] text-white">
            Every conversation, lead and booking in one calm place.
          </p>
          <ul className="mt-6 flex flex-col gap-3 text-[13.5px] text-sidebar-foreground/85">
            <li className="flex items-center gap-2.5">
              <span aria-hidden className="size-1.5 rounded-full bg-clay" />
              Review what your chatbot said, and step in when it matters
            </li>
            <li className="flex items-center gap-2.5">
              <span aria-hidden className="size-1.5 rounded-full bg-clay" />
              Follow every lead from first message to booked call
            </li>
            <li className="flex items-center gap-2.5">
              <span aria-hidden className="size-1.5 rounded-full bg-clay" />
              Tune your bot&apos;s knowledge, tone and hours
            </li>
          </ul>
        </div>
        <p className="text-[12px] text-sidebar-muted">Admin console · iSN Global Solutions</p>
        <div
          aria-hidden
          className="pointer-events-none absolute -right-24 -bottom-24 size-[360px] rounded-full border border-sidebar-border"
        />
        <div
          aria-hidden
          className="pointer-events-none absolute -right-8 -bottom-8 size-[220px] rounded-full border border-sidebar-border"
        />
      </aside>
      <main className="flex flex-1 items-center justify-center bg-background p-6">
        <div className="w-full max-w-[380px]">
          <div className="mb-8 flex items-center gap-2.5 lg:hidden">
            <BrandLogo height={24} />
            <span className="text-[15px] font-semibold">ChatLeads</span>
          </div>
          <h1 className="text-[24px] leading-tight font-semibold tracking-[-0.01em] text-foreground">{title}</h1>
          <p className="mt-1.5 text-[13.5px] text-muted-foreground">{description}</p>
          <div className="mt-7 rounded-[12px] border border-border bg-card p-6 shadow-[var(--shadow-card)]">{children}</div>
        </div>
      </main>
    </div>
  );
}
