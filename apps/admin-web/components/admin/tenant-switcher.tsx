"use client";

/**
 * Sidebar "active chatbot" switcher (multi-chatbot accounts) -- a compact
 * `<select>` that re-mints the session cookie on change via
 * `switchTenantAction` (called directly, not through a `<form>`; Next.js
 * server actions support `redirect()`/`revalidatePath()` either way).
 * Rendered only for CLIENT_ADMIN/CLIENT_AGENT (see `admin-shell.tsx`) --
 * PLATFORM_ADMIN has no account/switcher, they browse via `/clients/
 * {tenantId}/...` instead.
 *
 * Also the CLIENT_ADMIN-only entry point into `/chatbots/new` (a special
 * "+ Add a chatbot" option, user request) -- the standalone "My chatbots"
 * nav destination this used to live under was removed as redundant once
 * this dropdown covered list+switch; `/chatbots`/`/chatbots/new` themselves
 * are untouched, just reached from here now instead of the nav.
 *
 * Auto-applies on change rather than requiring a separate "Apply" click --
 * same UX decision as this app's `analytics-range.tsx` auto-submit fix
 * (a control that visually looks like it should apply instantly, applying
 * on a separate click instead, reads as broken).
 */
import { useTransition, type ChangeEvent } from "react";
import { useRouter } from "next/navigation";
import { switchTenantAction } from "@/app/(protected)/switch-tenant/actions";

export interface SwitchableChatbot {
  id: string;
  name: string;
  enabled: boolean;
}

/** Not a real tenant id (those are `uuid4().hex`, 32 lowercase hex chars) --
 *  a human-readable sentinel `<option>` value picked out in `handleChange`
 *  before it ever reaches `switchTenantAction`. */
const ADD_CHATBOT_VALUE = "__add_chatbot__";

export function TenantSwitcher({
  chatbots,
  activeTenantId,
  collapsed,
  canCreate,
}: {
  chatbots: SwitchableChatbot[];
  activeTenantId: string | null;
  collapsed: boolean;
  canCreate: boolean;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  // Nothing to show at all: no chatbots AND the caller can't create one
  // either (a CLIENT_AGENT on a still-empty account -- ask your admin).
  if (chatbots.length === 0 && !canCreate) return null;

  function handleChange(event: ChangeEvent<HTMLSelectElement>) {
    const value = event.target.value;
    if (value === ADD_CHATBOT_VALUE) {
      router.push("/chatbots/new");
      return;
    }
    if (value === activeTenantId) return;
    startTransition(() => {
      void switchTenantAction(value);
    });
  }

  return (
    <div className={collapsed ? "hidden" : "border-b border-border px-5 pb-3.5"}>
      <label
        htmlFor="tenant-switcher"
        className="mb-1 block text-[10.5px] font-semibold tracking-[0.12em] text-muted-foreground uppercase"
      >
        Active chatbot
      </label>
      <select
        id="tenant-switcher"
        defaultValue={activeTenantId ?? undefined}
        onChange={handleChange}
        disabled={isPending}
        className="w-full rounded-[9px] border border-border bg-white px-2.5 py-1.5 text-[13px] font-medium text-foreground outline-none disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        {chatbots.map((bot) => (
          <option key={bot.id} value={bot.id} disabled={!bot.enabled}>
            {bot.name}
            {!bot.enabled ? " (disabled)" : ""}
          </option>
        ))}
        {canCreate ? <option value={ADD_CHATBOT_VALUE}>+ Add a chatbot</option> : null}
      </select>
    </div>
  );
}
