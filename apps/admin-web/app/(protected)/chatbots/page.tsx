/**
 * "My chatbots" hub (multi-chatbot accounts) -- the CLIENT_ADMIN/
 * CLIENT_AGENT landing area for every chatbot on the caller's own account.
 * Mirrors `clients/page.tsx`'s tile-grid shape (PLATFORM_ADMIN's "Clients"
 * screen), but each tile's action SWITCHES the active chatbot (a plain,
 * zero-JS `<form action={switchTenantAction}>`) instead of linking into a
 * `[tenantId]` route -- that route-param pattern is platform-admin-only;
 * a client-side caller has no route param, only the cookie-derived active
 * tenant (see `switch-tenant/actions.ts`'s header comment).
 *
 * "Add a chatbot" is CLIENT_ADMIN-only (`POST /admin/tenants/mine`'s own
 * RBAC) -- a CLIENT_AGENT sees every chatbot and can switch between them,
 * but not create a new one.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { listMyChatbots, type ChatbotSummary } from "@/lib/chatbots";
import { switchTenantAction } from "@/app/(protected)/switch-tenant/actions";

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

function ChatbotCard({ chatbot, isActive }: { chatbot: ChatbotSummary; isActive: boolean }) {
  return (
    <li className="flex flex-col gap-3 rounded-[14px] border border-[var(--border)] bg-white p-[18px]">
      <div className="flex items-center gap-2.5">
        <div className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-[#ecece5] text-[13px] font-bold text-[var(--foreground)]">
          {initials(chatbot.name)}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-bold text-[var(--foreground)]">{chatbot.name}</p>
          <p className="truncate text-[11px] text-[var(--muted-foreground)]">{chatbot.slug}</p>
        </div>
        <StatusBadge enabled={chatbot.enabled} />
      </div>

      <div className="border-t border-[var(--secondary)] pt-3">
        {isActive ? (
          <span className="flex min-h-11 items-center justify-center rounded-lg bg-[var(--secondary)] px-3 text-[11.5px] font-semibold text-[var(--ink-2)]">
            Currently active
          </span>
        ) : (
          <form action={switchTenantAction.bind(null, chatbot.id)}>
            <button
              type="submit"
              disabled={!chatbot.enabled}
              className="flex min-h-11 w-full items-center justify-center rounded-lg border border-[var(--border)] px-3 text-[11.5px] font-semibold text-[var(--ink-2)] transition-colors hover:bg-[var(--secondary)] disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--foreground)]"
            >
              Switch to this chatbot →
            </button>
          </form>
        )}
      </div>
    </li>
  );
}

function AddChatbotTile() {
  return (
    <li>
      <Link
        href="/chatbots/new"
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

interface ChatbotsPageProps {
  searchParams: Promise<{ switchError?: string }>;
}

export default async function ChatbotsPage({ searchParams }: ChatbotsPageProps) {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");
  const canCreate = claims.role === "CLIENT_ADMIN";

  const params = await searchParams;
  const result = await listMyChatbots();

  return (
    <div className="flex flex-1 flex-col gap-6 p-6 lg:p-8">
      <div className="flex items-center gap-3.5">
        <div>
          <h1 className="text-xl font-bold text-[var(--foreground)]">My chatbots</h1>
          {result.status === "ok" ? (
            <p className="mt-0.5 text-[12.5px] text-[var(--muted-foreground)]">
              {result.items.length} chatbot{result.items.length === 1 ? "" : "s"} on your account
            </p>
          ) : null}
        </div>
        {canCreate ? (
          <Link
            href="/chatbots/new"
            className="ml-auto flex min-h-11 items-center whitespace-nowrap rounded-lg bg-[var(--foreground)] px-4 text-[12.5px] font-bold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--foreground)]"
          >
            + Add a chatbot
          </Link>
        ) : null}
      </div>

      {params.switchError ? (
        <p
          role="alert"
          className="rounded-[14px] border border-[var(--danger-fg)]/40 bg-[#f6e3df] p-4 text-sm text-[var(--danger-fg)]"
        >
          Couldn&apos;t switch chatbots ({params.switchError}). Please try again.
        </p>
      ) : null}

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
          {canCreate ? (
            <>
              No chatbots yet —{" "}
              <Link href="/chatbots/new" className="font-semibold underline underline-offset-2">
                add a chatbot
              </Link>{" "}
              to create the first one.
            </>
          ) : (
            "No chatbots on your account yet — ask your admin to add one."
          )}
        </p>
      ) : (
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {result.items.map((chatbot) => (
            <ChatbotCard
              key={chatbot.id}
              chatbot={chatbot}
              isActive={chatbot.id === claims.tenantId}
            />
          ))}
          {canCreate ? <AddChatbotTile /> : null}
        </ul>
      )}
    </div>
  );
}
