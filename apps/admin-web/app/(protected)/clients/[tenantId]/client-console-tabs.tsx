"use client";

/**
 * Section tabs for a specific client's platform-admin console
 * (Analytics | Reports | Knowledge | Leads -- Bot settings deliberately not
 * one of these, see `clients/[tenantId]/layout.tsx`'s own doc comment).
 * `"use client"` only for this small piece (`usePathname` for the active
 * indicator) -- the surrounding `layout.tsx` stays a server component,
 * matching this codebase's server-first default (`admin-shell.tsx`'s own
 * sidebar nav is the only other place this same split is used).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { slug: "analytics", label: "Analytics" },
  { slug: "reports", label: "Reports" },
  { slug: "knowledge", label: "Knowledge" },
  { slug: "leads", label: "Leads" },
] as const;

export function ClientConsoleTabs({ tenantId }: { tenantId: string }) {
  const pathname = usePathname();

  return (
    <nav aria-label="Client console sections" className="flex items-center gap-1 px-6">
      {TABS.map((tab) => {
        const href = `/clients/${tenantId}/${tab.slug}`;
        const active = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <Link
            key={tab.slug}
            href={href}
            aria-current={active ? "page" : undefined}
            className={`-mb-px border-b-2 px-3 py-2.5 text-[13px] font-semibold transition-colors ${
              active
                ? "border-foreground text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
