/**
 * Per-client knowledge upload screen (S13.7), restyled to match the 5a
 * design applied to `/knowledge` (see that page's header comment for the
 * full backend-reality trace: no list endpoint, so this stays a single-
 * source upload+status flow rather than a fabricated multi-row table).
 * Reuses S13.3's `UploadForm` as-is, parameterized by the route's
 * `{tenantId}` (D1) so the upload and status-poll actions target the S12.7
 * PLATFORM_ADMIN super-user surface `/admin/tenants/{tenantId}/ingestion/**`
 * instead of the implicit `/admin/ingestion/**`.
 */
import { UploadForm, CoverageCheckCard, TestBotCard } from "@/app/(protected)/knowledge/upload-form";

export default async function ClientKnowledgePage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;

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
          <CoverageCheckCard />
          <TestBotCard />
        </div>
      </div>
    </div>
  );
}
