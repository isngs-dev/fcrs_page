"use client";

/**
 * Platform-admin knowledge-doc row: the same card `doc-list.tsx` always
 * rendered, plus "View" (lazily fetches + expands the doc's status/latest
 * run/parsed-preview inline -- reuses `getDocStatus`, the SAME action the
 * upload-progress poll loop already calls, rather than adding a near-
 * duplicate "get detail" action) and "Export" (a plain link to the new
 * `/knowledge/download/[docId]` proxy route, mirroring `DownloadCsvLink`'s
 * simplicity -- no client-side fetch needed for a file download).
 *
 * `"use client"` only for this row -- `doc-list.tsx` stays a server
 * component and only reaches for this when `tenantId` is passed (the
 * platform-admin call site); the client-facing `/knowledge` page never
 * mounts this at all.
 */
import { useState } from "react";
import { SoftCard } from "@/components/admin/soft-card";
import { badgeToneClassName, statusBadge } from "@/lib/knowledge-constants";
import { cn } from "@/lib/utils";
import { formatUploadedAt } from "@/app/(protected)/knowledge/doc-list";
import {
  getDocStatus,
  type DocStatusResult,
  type KnowledgeDocListItem,
} from "@/app/(protected)/knowledge/actions";

const ACTION_LINK_CLASS =
  "text-[12px] font-semibold text-foreground underline underline-offset-2 hover:no-underline";

export function KnowledgeDocRow({
  doc,
  tenantId,
}: {
  doc: KnowledgeDocListItem;
  tenantId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<DocStatusResult | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleToggleView() {
    if (expanded) {
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (detail) return; // already fetched once -- don't re-fetch on every toggle.
    setLoading(true);
    const result = await getDocStatus(doc.docId, tenantId);
    setDetail(result);
    setLoading(false);
  }

  const badge = statusBadge(doc.status, null);
  const downloadHref = `/knowledge/download/${encodeURIComponent(doc.docId)}?tenant_id=${encodeURIComponent(tenantId)}`;

  return (
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

      <div className="mt-1 flex items-center gap-4">
        <button type="button" onClick={() => void handleToggleView()} className={ACTION_LINK_CLASS}>
          {expanded ? "Hide" : "View"}
        </button>
        <a href={downloadHref} className={ACTION_LINK_CLASS}>
          Export
        </a>
      </div>

      {expanded ? (
        <div className="mt-1 rounded-[9px] border border-border bg-background p-3 text-[12px] text-foreground">
          {loading ? (
            <p className="text-muted-foreground">Loading…</p>
          ) : detail?.status === "error" ? (
            <p role="alert" className="text-destructive">
              {detail.message}
            </p>
          ) : detail?.status === "ok" ? (
            <div className="flex flex-col gap-1.5">
              <p>
                <span className="font-semibold">Content type:</span> {doc.contentType}
              </p>
              {detail.run ? (
                <p>
                  <span className="font-semibold">Latest run:</span> {detail.run.status}
                  {detail.run.charsOut !== null ? ` · ${detail.run.charsOut} chars` : ""}
                </p>
              ) : null}
              {detail.parsedPreview ? (
                <div>
                  <p className="font-semibold">Preview</p>
                  <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap rounded-md bg-secondary p-2 text-[11.5px]">
                    {detail.parsedPreview}
                  </pre>
                </div>
              ) : (
                <p className="text-muted-foreground">No text preview available yet.</p>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </SoftCard>
  );
}
