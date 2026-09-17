/**
 * Knowledge Base list feature: renders every uploaded knowledge doc for the
 * tenant, newest upload first (server already sorts -- this never re-sorts).
 * Pure presentation fed the server-fetched `ListKnowledgeResult` -- no
 * "use client", no data fetching (server-first, mirrors leads-table.tsx's
 * pattern).
 *
 * Honest states only, no fabricated data (this screen's established norm --
 * see upload-form.tsx's CoverageCheckCard/TestBotCard): an empty tenant
 * shows a real "no items yet" message, never placeholder rows; a fetch
 * failure shows the real error, never a silently blank list.
 *
 * `tenantId` (platform-admin knowledge redesign): when provided, each row
 * renders via `<KnowledgeDocRow>` (a small `"use client"` component) instead
 * of the plain server-rendered card below, adding View/Export actions. Left
 * `undefined` (the client-facing `/knowledge` call site), this component
 * stays a server component -- the card itself is still server-rendered, but
 * each row now embeds `<DeleteDocButton>` (delete-a-knowledge-doc feature),
 * a small client child for the delete confirm/action only. Platform admins
 * never get this control (deliberately -- matches this app's established
 * "write capability into a client's tenant is CLIENT_ADMIN-only" precedent).
 */
import { SoftCard } from "@/components/admin/soft-card";
import { badgeToneClassName, statusBadge } from "@/lib/knowledge-constants";
import { cn } from "@/lib/utils";
import { KnowledgeDocRow } from "@/app/(protected)/knowledge/knowledge-doc-row";
import { DeleteDocButton } from "@/app/(protected)/knowledge/delete-doc-button";
import type { ListKnowledgeResult } from "@/app/(protected)/knowledge/actions";

export function formatUploadedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function KnowledgeDocList({
  result,
  tenantId,
}: {
  result: ListKnowledgeResult;
  tenantId?: string;
}) {
  if (result.status === "error") {
    return (
      <div
        role="alert"
        className="rounded-[9px] border border-[var(--danger-border)] bg-[#f6e3df] p-3 text-[12.5px] font-medium text-[var(--danger-fg)]"
      >
        Unable to load knowledge items. {result.message}
      </div>
    );
  }

  if (result.docs.length === 0) {
    return (
      <p
        role="status"
        className="rounded-[9px] border border-border bg-background p-4 text-[12.5px] text-muted-foreground"
      >
        No knowledge items yet — upload one above.
      </p>
    );
  }

  return (
    <ul className="flex flex-col gap-3">
      {result.docs.map((doc) => {
        if (tenantId) {
          return (
            <li key={doc.docId}>
              <KnowledgeDocRow doc={doc} tenantId={tenantId} />
            </li>
          );
        }

        const badge = statusBadge(doc.status, null);
        return (
          <li key={doc.docId}>
            <SoftCard className="flex flex-col gap-1.5 p-4">
              <div className="flex items-start justify-between gap-3">
                <p className="min-w-0 truncate text-[14px] font-semibold text-foreground">
                  {doc.title || doc.filename}
                </p>
                <span
                  className={cn(
                    "inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[10.5px] font-bold whitespace-nowrap",
                    badgeToneClassName(badge.tone)
                  )}
                >
                  {badge.tone === "success" ? "●" : badge.tone === "failed" ? "✕" : "◌"}{" "}
                  {badge.label.toUpperCase()}
                </span>
              </div>
              {doc.description ? (
                <p className="text-[12.5px] text-muted-foreground">{doc.description}</p>
              ) : null}
              <p className="text-[11.5px] text-muted-foreground">
                {doc.title ? `${doc.filename} · ` : ""}
                Uploaded {formatUploadedAt(doc.createdAt)}
                {doc.uploadedByName ? ` by ${doc.uploadedByName}` : ""}
              </p>
              <div className="mt-1">
                <DeleteDocButton docId={doc.docId} label={doc.title || doc.filename} />
              </div>
            </SoftCard>
          </li>
        );
      })}
    </ul>
  );
}
