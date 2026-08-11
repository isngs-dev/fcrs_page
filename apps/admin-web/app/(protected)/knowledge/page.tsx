/**
 * Knowledge upload screen (S13.3), restyled to the locked 5a design
 * (knowledge_base/ui design/updated ui/project/HANDOFF-SPEC.md §3
 * "5a Knowledge"). CLIENT_ADMIN-only -- gated by `requireRole` (decision 2),
 * colocated with this screen rather than a `proxy.ts` route->role map,
 * matching the S13.2 pattern. This intentionally excludes both
 * PLATFORM_ADMIN and CLIENT_AGENT: the backend's
 * `require_roles(Role.CLIENT_ADMIN)` (routes.py:49) is an exact allowlist,
 * not hierarchical.
 *
 * Layout note: the 5a mock shows a full sources table (multiple rows,
 * chunk counts, mixed source types) fed by a real list endpoint. The actual
 * backend (services/api/src/api/ingestion/routes.py) only exposes
 * `GET /admin/ingestion/docs/{doc_id}` -- a single document by id, no list
 * route, no chunk count in the response. So this page keeps the upload +
 * single-source-status flow it already had, restyled to the 5a visual
 * recipe (header meta line, dashed dropzone, source-row status card,
 * Coverage check + Test the bot side cards) rather than fabricating a
 * multi-row table the backend can't back.
 *
 * SR-27 slice 3: geometry-only restyle to `Console.dc.html:458-499` -- fixed
 * 360px right column (was a fractional `2.2fr_1fr` track), 22px/24px card
 * padding, 28px/600 title. Both honest gap states (Coverage check, Test the
 * bot) preserved verbatim -- only their card geometry changed.
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { UploadForm, CoverageCheckCard, TestBotCard } from "@/app/(protected)/knowledge/upload-form";
import { SoftCard } from "@/components/admin/soft-card";

export default async function KnowledgePage() {
  await requireRole("CLIENT_ADMIN");

  return (
    <div className="flex flex-1 flex-col gap-5 p-5 sm:p-7">
      <div className="flex flex-wrap items-start gap-3.5">
        <div>
          <Link
            href="/"
            className="mb-1.5 inline-block text-[12.5px] text-muted-foreground hover:text-foreground hover:underline"
          >
            ← Back to console
          </Link>
          <h1 className="text-[28px] font-semibold text-foreground">Knowledge base</h1>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            What your bot knows. Upload a document below to add to it.
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-[18px] lg:flex-row lg:items-start">
        <SoftCard className="flex-1 px-6 pb-2 pt-1">
          <h2 className="pt-4 pb-0.5 text-[16px] font-semibold text-foreground">Upload knowledge</h2>
          <p className="mb-[18px] mt-1.5 text-[12.5px] leading-relaxed text-muted-foreground">
            .txt or .docx, up to 10 MiB. It is parsed, chunked, and embedded asynchronously -- the
            status card below tracks the run&apos;s progress in real time.
          </p>
          <div className="pb-2">
            <UploadForm />
          </div>
        </SoftCard>

        <div className="flex w-full flex-none flex-col gap-[18px] lg:w-[360px]">
          <CoverageCheckCard />
          <TestBotCard />
        </div>
      </div>
    </div>
  );
}
