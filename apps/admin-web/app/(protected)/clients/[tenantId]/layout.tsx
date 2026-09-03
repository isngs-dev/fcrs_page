/**
 * Per-client management area layout (S13.7 D2). Renders a calm, breadcrumb-
 * style "Clients / <name>" header + the D7 rotate-key control on every
 * `/clients/{tenantId}/…` screen -- deliberately NOT a loud "you are
 * managing X, all changes audited" banner (D2 rejects that framing
 * explicitly; the honest audit trail is server-side regardless, S12.7 D4).
 *
 * The client's display name is resolved server-side via `getClient()`
 * (lib/clients.ts), never from a link param or client state (D3) -- so the
 * header can never disagree with which tenant the child screens are
 * actually calling through their own `tenantId` prop (also read from this
 * same route segment, D1).
 *
 * An unknown/inactive `{tenantId}` is an honest not-found here; the S12.7
 * tenant-scoped data routes independently 404/403 regardless of what this
 * layout renders (defense-in-depth only, per the Constraints section).
 *
 * Product decision: platform admins no longer see or edit a client's bot
 * settings at all (that's exclusively a CLIENT_ADMIN capability now) --
 * `clients/[tenantId]/settings/**` was deleted rather than kept reachable.
 * The `<ClientConsoleTabs>` bar below (Analytics | Reports | Knowledge |
 * Leads) is this layout's first real navigation between sibling per-client
 * pages -- there was none before, tolerable at 4 direct-URL-only routes,
 * not once Reports grew to 9.
 */
import Link from "next/link";
import { notFound } from "next/navigation";
import { requireRole } from "@/lib/auth";
import { getClient } from "@/lib/clients";
import { RotateKeyControl } from "@/app/(protected)/clients/[tenantId]/rotate-key-control";
import { ClientConsoleTabs } from "@/app/(protected)/clients/[tenantId]/client-console-tabs";

export default async function ClientLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ tenantId: string }>;
}) {
  await requireRole("PLATFORM_ADMIN");

  const { tenantId } = await params;
  const result = await getClient(tenantId);

  if (result.status === "not_found") {
    notFound();
  }

  return (
    <div className="flex flex-1 flex-col">
      <header className="flex items-center justify-between border-b border-input px-6 py-3">
        <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 text-sm">
          <Link href="/clients" className="text-muted-foreground hover:underline">
            Clients
          </Link>
          <span className="text-muted-foreground">/</span>
          {result.status === "ok" ? (
            <span className="font-medium">{result.client.name}</span>
          ) : (
            <span className="text-destructive">
              {result.message}
              {result.correlationId ? ` (${result.correlationId})` : ""}
            </span>
          )}
        </nav>
        <RotateKeyControl tenantId={tenantId} />
      </header>
      <div className="border-b border-input">
        <ClientConsoleTabs tenantId={tenantId} />
      </div>
      <div className="flex flex-1 flex-col">{children}</div>
    </div>
  );
}
