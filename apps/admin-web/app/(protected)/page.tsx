/**
 * SR-16: the console's landing page, rebuilt as a "chatbot performance"
 * dashboard (D1/D2). The lead-pipeline kanban (`StageDistributionChart`,
 * `PipelineColumn`, `LeadCard`, both pipeline `<section>`s, and the
 * `getDashboardPipeline()` call) is DELETED entirely per D1 -- not moved
 * below the fold. Leads now live at `/leads` (SR-15 restyle; SR-18 builds
 * the real board there). `lib/dashboard.ts` is intentionally NOT deleted --
 * see its own file header, which now names this sprint's orphan finding.
 *
 * Composed entirely from SR-15's primitives (`SoftCard`, tokens, Archivo)
 * and the already-shipped `getChatbotHub()` data layer (M1) -- no new
 * fetch, no new endpoint, no new dependency (D6/D8, CLAUDE.md §4).
 */
import { redirect } from "next/navigation";
import { CircleAlert } from "lucide-react";
import { getClaims } from "@/lib/auth";
import { getBotSettings } from "@/lib/settings";
import { getChatbotHub } from "@/lib/hub";
import { DashboardHero } from "@/app/(protected)/dashboard-hero";
import { ResponsesOverTimeCard } from "@/app/(protected)/dashboard-responses-card";
import { DashboardMetricCards } from "@/app/(protected)/dashboard-metric-cards";

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

interface ProtectedHomePageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

export default async function ProtectedHomePage({ searchParams }: ProtectedHomePageProps) {
  const claims = await getClaims();
  if (!claims) {
    redirect("/login");
  }
  // PLATFORM_ADMIN's landing behaviour is explicitly preserved, unchanged
  // by this rewrite (SR-16 DoD D12) -- a bot-performance dashboard is
  // inherently tenant-scoped, so the platform operator's landing route
  // stays `/clients` rather than rendering a single tenant's numbers.
  if (claims.role === "PLATFORM_ADMIN") redirect("/clients");
  if (claims.role !== "CLIENT_ADMIN" && claims.role !== "CLIENT_AGENT") redirect("/login");

  const params = await searchParams;
  const period = firstValue(params.period);
  const bucket = firstValue(params.bucket);
  const [settingsResult, hubResult] = await Promise.all([
    getBotSettings(),
    getChatbotHub({ period, bucket }),
  ]);
  const dashboardTitle =
    settingsResult.status === "ok" && settingsResult.settings.dashboardTitle?.trim()
      ? settingsResult.settings.dashboardTitle
      : "Dashboard";

  return (
    <main className="flex flex-1 flex-col gap-6 px-4 py-8 sm:px-6 lg:px-7 lg:py-10">
      {hubResult.status === "error" ? (
        <div role="alert" className="rounded-2xl border border-[var(--danger-border)] bg-[#fff3ee] p-4 text-sm text-[var(--danger-fg)]">
          <div className="flex gap-2">
            <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
            <div>
              <p className="font-semibold">The chatbot hub could not be loaded.</p>
              <p className="mt-1">{hubResult.message}</p>
              {hubResult.correlationId ? <p className="mt-1 text-xs">Correlation ID: {hubResult.correlationId}</p> : null}
            </div>
          </div>
        </div>
      ) : (
        <>
          <DashboardHero hub={hubResult.data} title={dashboardTitle} />
          <ResponsesOverTimeCard hub={hubResult.data} />
          <DashboardMetricCards hub={hubResult.data} />
        </>
      )}
    </main>
  );
}

