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
 */
import { SoftCard } from "@/components/admin/soft-card";
import { badgeToneClassName, statusBadge } from "@/lib/knowledge-constants";
import { cn } from "@/lib/utils";
import type { ListKnowledgeResult } from "@/app/(protected)/knowledge/actions";

function formatUploadedAt(iso: string): string {
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

export function KnowledgeDocList({ result }: { result: ListKnowledgeResult }) {
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
            </SoftCard>
          </li>
        );
      })}
    </ul>
  );
}
