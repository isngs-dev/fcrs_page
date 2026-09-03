/**
 * Per-client knowledge screen (S13.7). Product decision: platform admins no
 * longer upload into a client's knowledge base -- that's exclusively a
 * CLIENT_ADMIN capability now (mirrors the same "platform admin loses a
 * write capability" decision already made for Bot settings). This screen is
 * read-only oversight: the uploaded-knowledge list (`listKnowledgeDocs`,
 * still the S12.7 PLATFORM_ADMIN tenant-scoped surface), each item now with
 * "View" (an inline expand showing status/latest run/parsed-text preview,
 * via `<KnowledgeDocRow>`) and "Export" (downloads the original file via the
 * new `/knowledge/download/[docId]` proxy route) -- see `doc-list.tsx`'s and
 * `knowledge-doc-row.tsx`'s own doc comments for the `tenantId`-gated reuse
 * pattern. `UploadForm` is gone from this screen entirely; the client-facing
 * `/knowledge` page is untouched and keeps it.
 *
 * Train the Agent feature: `listCoverageGaps(tenantId)`/`<TestBotChat
 * tenantId>` are untouched (not named in the request) -- still the same
 * PLATFORM_ADMIN tenant-scoped surface pattern.
 */
import { listCoverageGaps, listKnowledgeDocs } from "@/app/(protected)/knowledge/actions";
import { KnowledgeDocList } from "@/app/(protected)/knowledge/doc-list";
import { CoverageGaps } from "@/app/(protected)/knowledge/coverage-gaps";
import { TestBotChat } from "@/app/(protected)/knowledge/test-bot-chat";

export default async function ClientKnowledgePage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const docsResult = await listKnowledgeDocs(tenantId);
  const gapsResult = await listCoverageGaps(tenantId);

  return (
    <div className="flex flex-1 flex-col gap-5 p-5 sm:p-7">
      <div>
        <h1 className="text-[20px] font-bold text-[var(--foreground)]">Knowledge base</h1>
        <p className="mt-0.5 text-[12.5px] text-[var(--muted-foreground)]">
          What this client&apos;s bot knows -- view or export what has been uploaded.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4.5 lg:grid-cols-[2.2fr_1fr] lg:items-start">
        <div className="rounded-[14px] border border-[var(--border)] bg-white p-4.5 sm:p-5">
          <h2 className="mb-3.5 text-[14px] font-bold text-[var(--foreground)]">Uploaded knowledge</h2>
          <p className="mb-4 text-[12.5px] text-[var(--muted-foreground)]">
            Every knowledge item on file for this client&apos;s bot, newest upload first.
          </p>
          <KnowledgeDocList result={docsResult} tenantId={tenantId} />
        </div>

        <div className="flex flex-col gap-3.5">
          <CoverageGaps result={gapsResult} tenantId={tenantId} />
          <TestBotChat tenantId={tenantId} />
        </div>
      </div>
    </div>
  );
}
