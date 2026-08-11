"use client";

/**
 * SR-21 scope item 10: per-item "mark read" control, and the page's "Mark
 * all read" action. Both are thin client wrappers around the server actions
 * in `actions/notifications-actions.ts` -- `useTransition` for in-flight
 * state (mirrors `leads-board.tsx`'s drop-handler pattern) and
 * `router.refresh()` on success so the (server-rendered) list re-fetches
 * without a full page reload.
 */
import { useRouter } from "next/navigation";
import { useTransition } from "react";
import {
  markAllNotificationsRead,
  markNotificationRead,
} from "@/app/(protected)/actions/notifications-actions";

export function MarkReadButton({
  eventId,
  read,
  revalidatePathTarget,
}: {
  eventId: string;
  read: boolean;
  revalidatePathTarget: string;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  if (read) {
    return null;
  }

  return (
    <button
      type="button"
      disabled={isPending}
      onClick={() => {
        startTransition(async () => {
          await markNotificationRead(eventId, true, revalidatePathTarget);
          router.refresh();
        });
      }}
      className="rounded-full border border-border px-2.5 py-1 text-[11px] font-semibold text-[var(--ink-2)] hover:bg-secondary disabled:opacity-50"
    >
      {isPending ? "Marking…" : "Mark read"}
    </button>
  );
}

export function MarkAllReadButton({
  category,
  revalidatePathTarget,
}: {
  category: "leads" | "system" | undefined;
  revalidatePathTarget: string;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  return (
    <button
      type="button"
      disabled={isPending}
      onClick={() => {
        startTransition(async () => {
          await markAllNotificationsRead(category, revalidatePathTarget);
          router.refresh();
        });
      }}
      className="rounded-full border border-border px-3 py-1.5 text-[11.5px] font-semibold text-[var(--ink-2)] hover:bg-secondary disabled:opacity-50"
    >
      {isPending ? "Marking all…" : "Mark all read"}
    </button>
  );
}
