"use client";

/**
 * Thin client adapter wiring the Contact record onto the shared
 * `RecordDrawer` (D3) -- URL navigation (open/close/`?before=`) lives here;
 * rendering lives in `record-drawer.tsx`. Mirrors `lead-drawer.tsx`'s
 * `navigate`/`close` shape exactly, minus tabs (contacts have no
 * Transcript/Details/Activity/Notes split -- SR-17 gives them the one
 * timeline-shaped view M8 specifies).
 */
import { useCallback } from "react";
import { useRouter } from "next/navigation";
import type { ContactDetailResult } from "@/lib/contacts";
import type { TimelineFetchResult } from "@/lib/timeline";
import { RecordDrawer, type RecordDrawerSubject } from "@/components/admin/record-drawer";

export function ContactDrawer({
  contactId,
  detailResult,
  timelineResult,
  basePath,
}: {
  contactId: string;
  detailResult: ContactDetailResult;
  timelineResult: TimelineFetchResult;
  basePath: string;
}) {
  const router = useRouter();

  const close = useCallback(() => {
    router.push(basePath, { scroll: false });
  }, [router, basePath]);

  if (detailResult.status === "error") {
    return (
      <RecordDrawer
        subject={{ kind: "contact", id: contactId, name: null, secondary: null }}
        result={{ status: "error", message: detailResult.message, correlationId: detailResult.correlationId }}
        onClose={close}
        loadOlderHref={null}
      />
    );
  }

  const { contact } = detailResult;
  const subject: RecordDrawerSubject = {
    kind: "contact",
    id: contactId,
    name: contact.name,
    secondary: [contact.email, contact.phone].filter(Boolean).join(" · ") || null,
    accountId: contact.accountId,
  };

  const loadOlderHref =
    timelineResult.status === "ok" && timelineResult.data.nextBefore
      ? `${basePath}?contact=${encodeURIComponent(contactId)}&before=${encodeURIComponent(
          timelineResult.data.nextBefore
        )}`
      : null;

  return (
    <RecordDrawer subject={subject} result={timelineResult} onClose={close} loadOlderHref={loadOlderHref} />
  );
}
