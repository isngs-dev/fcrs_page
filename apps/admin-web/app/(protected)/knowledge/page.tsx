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
 * Knowledge Base list feature: `GET /admin/ingestion/docs` now exists, so
 * this page fetches the real list server-side (`listKnowledgeDocs`,
 * `adminApiFetch` is always `cache: "no-store"`) and renders it via
 * `<KnowledgeDocList>` below the upload card, sorted newest-first exactly
 * as the backend returns it. `uploadKnowledge` (actions.ts) calls
 * `revalidatePath("/knowledge")` on a fresh upload, so this list reflects
 * the new item immediately without a manual reload -- no client-side
 * polling/fetching needed here, this stays a plain server-rendered list.
 *
 * Train the Agent feature: the former "Coverage check"/"Test the bot"
 * honest-placeholder cards are now real. `listCoverageGaps` fetches recent
 * unanswered visitor questions server-side and feeds `<CoverageGaps>`;
 * `<TestBotChat>` is a client component driving the stateless preview chat
 * itself. Teaching an answer (either surface) calls
 * `submitTrainedAnswer`, which `revalidatePath("/knowledge")`s -- same
 * refresh contract as `uploadKnowledge`.
 *
 * SR-27 slice 3: geometry-only restyle to `Console.dc.html:458-499` -- fixed
 * 360px right column (was a fractional `2.2fr_1fr` track), 22px/24px card
 * padding, 28px/600 title.
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { listCoverageGaps, listKnowledgeDocs } from "@/app/(protected)/knowledge/actions";
import { UploadForm } from "@/app/(protected)/knowledge/upload-form";
import { KnowledgeDocList } from "@/app/(protected)/knowledge/doc-list";
import { CoverageGaps } from "@/app/(protected)/knowledge/coverage-gaps";
import { TestBotChat } from "@/app/(protected)/knowledge/test-bot-chat";
import { SoftCard } from "@/components/admin/soft-card";

export default async function KnowledgePage() {
  await requireRole("CLIENT_ADMIN");
  const docsResult = await listKnowledgeDocs();
  const gapsResult = await listCoverageGaps();

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
          <CoverageGaps result={gapsResult} />
          <TestBotChat />
        </div>
      </div>

      <SoftCard className="flex-1 px-6 pb-6 pt-4">
        <h2 className="pb-0.5 text-[16px] font-semibold text-foreground">Uploaded knowledge</h2>
        <p className="mb-[18px] mt-1.5 text-[12.5px] leading-relaxed text-muted-foreground">
          Every knowledge item on file for this bot, newest upload first.
        </p>
        <KnowledgeDocList result={docsResult} />
      </SoftCard>
    </div>
  );
}
