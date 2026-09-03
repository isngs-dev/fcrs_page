/**
 * PLATFORM_ADMIN "Clients" tile list (S13.7), restyled to the locked design
 * spec screen 6b (`knowledge_base/ui design/updated ui/project/
 * Chatbot System Designs.dc.html#6b` + `HANDOFF-SPEC.md` §2/§3). This is the
 * top-level area a platform admin lands on (D4) -- a tenant card per
 * onboarded client, each linking into that client's management area
 * (`/clients/{tenantId}/settings`), plus links to the dedicated "Add a
 * chatbot" screen (`/clients/new`, also reachable from the sidebar's Clients
 * nav entry) for the platform-level onboarding action. Gated by `requireRole`
 * (D6) -- a CLIENT_ADMIN/CLIENT_AGENT who forces this URL is redirected to
 * their own dashboard.
 *
 * Design-vs-real-data note (read before "fixing" the status/usage fields):
 * screen 6b's mockup shows ACTIVE/ONBOARDING/PAST DUE badges, a usage row
 * (convos/mo, leads, plan), and an ONBOARDING checklist card -- none of that
 * has a backend source. `ClientSummary` (lib/clients.ts) only carries
 * `tenantId`/`name`/`slug`/`enabled` (from `TenantRepository.list`, no
 * billing/usage/plan/checklist columns exist -- confirmed against
 * `services/api/src/api/tenants/**` and `services/api/src/api/admin/**`).
 * So this page renders the real two-state signal the backend actually has --
 * ACTIVE vs DISABLED, from `enabled` -- and deliberately OMITS the usage row
 * and the ONBOARDING checklist card rather than fabricate numbers or fake
 * checklist progress (CLAUDE.md §3 "no silent fallbacks" / honest empty
 * states, same standard `listClients()` already applies to the empty-list
 * case). Promoting a real `/admin/tenants` list with usage/billing/plan
 * fields is a reasonable follow-up but out of scope here (no `services/**`
 * changes this sprint).
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { listClients, type ClientSummary } from "@/lib/clients";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "??";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function StatusBadge({ enabled }: { enabled: boolean }) {
  return (
    <span
      className={
        "shrink-0 rounded-full px-2 py-[3px] text-[10px] font-bold tracking-wide " +
        (enabled ? "bg-[#dcefdc] text-[var(--success-fg)]" : "bg-[#f6e3df] text-[var(--danger-fg)]")
      }
    >
      {enabled ? "ACTIVE" : "DISABLED"}
    </span>
  );
}

function ClientCard({ client }: { client: ClientSummary }) {
  return (
    <li className="flex flex-col gap-3 rounded-[14px] border border-[var(--border)] bg-white p-[18px]">
      <div className="flex items-center gap-2.5">
        <div className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-[#ecece5] text-[13px] font-bold text-[var(--foreground)]">
          {initials(client.name)}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-bold text-[var(--foreground)]">{client.name}</p>
          <p className="truncate text-[11px] text-[var(--muted-foreground)]">{client.slug}</p>
        </div>
        <StatusBadge enabled={client.enabled} />
      </div>

      {/* Usage row intentionally omitted -- no backend usage/plan/billing
          signal exists for tenants yet (see file header note). */}

      <div className="flex gap-2 border-t border-[var(--secondary)] pt-3">
        <Link
          href={`/clients/${client.tenantId}/analytics`}
          className="flex min-h-11 flex-1 items-center justify-center rounded-lg border border-[var(--border)] px-3 text-[11.5px] font-semibold text-[var(--ink-2)] transition-colors hover:bg-[var(--secondary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--foreground)]"
        >
          Open console →
        </Link>
      </div>
    </li>
  );
}

function AddChatbotTile() {
  return (
    <li>
      <Link
        href="/clients/new"
        className="flex min-h-[150px] w-full flex-col items-center justify-center gap-2 rounded-[14px] border-[1.5px] border-dashed border-[#d5d5cb] text-[var(--muted-foreground)] transition-colors hover:border-[#a8a99f] hover:text-[var(--muted-foreground)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--foreground)]"
      >
        <span className="grid size-[34px] place-items-center rounded-full bg-[var(--secondary)] text-base">
          +
        </span>
        <span className="text-xs font-semibold">Add a chatbot</span>
      </Link>
    </li>
  );
}

export default async function ClientsPage() {
  await requireRole("PLATFORM_ADMIN");

  const result = await listClients();
  const activeCount =
    result.status === "ok" ? result.items.filter((c) => c.enabled).length : null;

  return (
    <div className="flex flex-1 flex-col gap-6 p-6 lg:p-8">
      <div className="flex items-center gap-3.5">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-xl font-bold text-[var(--foreground)]">Clients</h1>
            {/* SR-15 D1: the PLATFORM ADMIN pill's citron text is deleted and
                re-decided to white, matching the shell's identical
                re-decision for dark-filled pills/buttons. */}
            <span className="rounded-full bg-[var(--foreground)] px-2.5 py-[3px] text-[10.5px] font-bold text-white">
              PLATFORM ADMIN
            </span>
          </div>
          {result.status === "ok" ? (
            <p className="mt-0.5 text-[12.5px] text-[var(--muted-foreground)]">
              {result.items.length} tenant{result.items.length === 1 ? "" : "s"}
              {activeCount !== null ? ` · ${activeCount} active` : ""}
            </p>
          ) : null}
        </div>
        <Link
          href="/clients/new"
          className="ml-auto flex min-h-11 items-center whitespace-nowrap rounded-lg bg-[var(--foreground)] px-4 text-[12.5px] font-bold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--foreground)]"
        >
          + Add a chatbot
        </Link>
      </div>

      {result.status === "error" ? (
        <p
          role="alert"
          className="rounded-[14px] border border-[var(--danger-fg)]/40 bg-[#f6e3df] p-4 text-sm text-[var(--danger-fg)]"
        >
          {result.message}
          {result.correlationId ? (
            <span className="block text-xs opacity-80">Correlation ID: {result.correlationId}</span>
          ) : null}
        </p>
      ) : result.items.length === 0 ? (
        <p role="status" className="rounded-[14px] border border-[var(--border)] bg-[var(--secondary)] p-4 text-sm text-[var(--ink-2)]">
          No chatbots yet —{" "}
          <Link href="/clients/new" className="font-semibold underline underline-offset-2">
            add a chatbot
          </Link>{" "}
          to onboard the first one.
        </p>
      ) : (
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {result.items.map((client) => (
            <ClientCard key={client.tenantId} client={client} />
          ))}
          <AddChatbotTile />
        </ul>
      )}
    </div>
  );
}
