/**
 * Per-client knowledge upload screen (S13.7), restyled to match the 5a
 * design applied to `/knowledge`. Reuses S13.3's `UploadForm` as-is,
 * parameterized by the route's `{tenantId}` (D1) so the upload and
 * status-poll actions target the S12.7 PLATFORM_ADMIN super-user surface
 * `/admin/tenants/{tenantId}/ingestion/**` instead of the implicit
 * `/admin/ingestion/**`.
 *
 * Knowledge Base list feature: `listKnowledgeDocs(tenantId)` targets the
 * same PLATFORM_ADMIN tenant-scoped list surface; `uploadKnowledge`
 * (actions.ts) calls `revalidatePath(`/clients/${tenantId}/knowledge`)` on
 * a fresh upload (bound via `.bind(null, tenantId)`), so the list below
 * reflects the new item immediately without a manual reload -- mirrors
 * `/knowledge`'s own header comment for the full rationale.
 *
 * Train the Agent feature: `listCoverageGaps(tenantId)`/`<TestBotChat
 * tenantId>` mirror the same PLATFORM_ADMIN tenant-scoped surface pattern.
 */
import { listCoverageGaps, listKnowledgeDocs } from "@/app/(protected)/knowledge/actions";
import { UploadForm } from "@/app/(protected)/knowledge/upload-form";
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
          What this client&apos;s bot knows. Upload a document below to add to it.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4.5 lg:grid-cols-[2.2fr_1fr] lg:items-start">
        <div className="rounded-[14px] border border-[var(--border)] bg-white p-4.5 sm:p-5">
          <h2 className="mb-3.5 text-[14px] font-bold text-[var(--foreground)]">Upload knowledge</h2>
          <p className="mb-4 text-[12.5px] text-[var(--muted-foreground)]">
            .txt or .docx, up to 10 MiB, uploaded for this client. It is parsed, chunked, and
            embedded asynchronously -- the status card below tracks the run&apos;s progress in
            real time.
          </p>
          <UploadForm tenantId={tenantId} />
        </div>

        <div className="flex flex-col gap-3.5">
          <CoverageGaps result={gapsResult} tenantId={tenantId} />
          <TestBotChat tenantId={tenantId} />
        </div>
      </div>

      <div className="rounded-[14px] border border-[var(--border)] bg-white p-4.5 sm:p-5">
        <h2 className="mb-3.5 text-[14px] font-bold text-[var(--foreground)]">Uploaded knowledge</h2>
        <p className="mb-4 text-[12.5px] text-[var(--muted-foreground)]">
          Every knowledge item on file for this client&apos;s bot, newest upload first.
        </p>
        <KnowledgeDocList result={docsResult} />
      </div>
    </div>
  );
}
