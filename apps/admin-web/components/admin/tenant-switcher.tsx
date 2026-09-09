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
 * Auto-applies on change rather than requiring a separate "Apply" click --
 * same UX decision as this app's `analytics-range.tsx` auto-submit fix
 * (a control that visually looks like it should apply instantly, applying
 * on a separate click instead, reads as broken).
 */
import { useTransition, type ChangeEvent } from "react";
import { switchTenantAction } from "@/app/(protected)/switch-tenant/actions";

export interface SwitchableChatbot {
  id: string;
  name: string;
  enabled: boolean;
}

export function TenantSwitcher({
  chatbots,
  activeTenantId,
  collapsed,
}: {
  chatbots: SwitchableChatbot[];
  activeTenantId: string | null;
  collapsed: boolean;
}) {
  const [isPending, startTransition] = useTransition();

  if (chatbots.length === 0) return null;

  function handleChange(event: ChangeEvent<HTMLSelectElement>) {
    const tenantId = event.target.value;
    if (tenantId === activeTenantId) return;
    startTransition(() => {
      void switchTenantAction(tenantId);
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
      </select>
    </div>
  );
}
