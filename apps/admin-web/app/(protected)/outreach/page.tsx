/**
 * Outreach overview (A8 U3): is Rebecca running, what needs a person, what
 * happened this week, and (admins) this month's spend vs the cap. Items that
 * need a human already land in Notifications (`agent_*_needs_human`) -- linked
 * here, not duplicated.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { getOverview } from "@/lib/outreach";
import { COST_LABELS, formatMoney, humanize } from "@/lib/outreach-format";
import { SoftCard } from "@/components/admin/soft-card";
import { Chip } from "@/components/admin/chip";
import { OutreachHeader, ErrorCard } from "@/app/(protected)/outreach/outreach-ui";

function Stat({ label, value, href }: { label: string; value: number | string; href?: string }) {
  const body = (
    <SoftCard className="flex flex-col gap-1 px-5 py-4">
      <span className="text-[12px] font-medium text-muted-foreground">{label}</span>
      <span className="text-[26px] font-semibold text-foreground">{value}</span>
    </SoftCard>
  );
  return href ? (
    <Link href={href} className="no-underline">
      {body}
    </Link>
  ) : (
    body
  );
}

export default async function OutreachOverviewPage() {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");
  const result = await getOverview();

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Outreach" subtitle="What Rebecca's email and calling agents are doing." />
      {result.status === "error" ? (
        <ErrorCard message={result.message} correlationId={result.correlationId} />
      ) : (
        <>
          <SoftCard className="flex flex-wrap items-center gap-3 px-5 py-4">
            {result.overview.platformPaused ? (
              <Chip className="bg-[#fde8e8] text-[#9b1c1c]">Paused by the platform</Chip>
            ) : result.overview.outreachPaused ? (
              <Chip className="bg-[#fde8e8] text-[#9b1c1c]">Paused</Chip>
            ) : (
              <Chip tone="success" dot>
                Running
              </Chip>
            )}
            <span className="text-[13px] text-muted-foreground">
              Channels:{" "}
              {result.overview.enabledChannels.length
                ? result.overview.enabledChannels.map(humanize).join(", ")
                : "none enabled"}
              {" · "}Email mode: {humanize(result.overview.emailMode)}
            </span>
            <Link href="/notifications?category=leads" className="ml-auto text-[13px] font-semibold">
              Needs a person → Notifications
            </Link>
          </SoftCard>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Drafts awaiting review" value={result.overview.draftsAwaiting} href="/outreach/emails?status=draft" />
            <Stat label="Active follow-up plans" value={result.overview.activePlans} />
            <Stat label="Steps due in 24 hours" value={result.overview.tasksDue24h} />
            <Stat label="Emails sent (7 days)" value={result.overview.emailsSent7d} href="/outreach/emails?status=sent" />
            <Stat label="Calls (7 days)" value={result.overview.calls7d} href="/outreach/calls" />
            <Stat label="Booked on a call (7 days)" value={result.overview.callsBooked7d} />
          </div>

          {result.overview.costs ? (
            <SoftCard className="flex flex-col gap-2 px-5 py-4">
              <h2 className="text-[15px] font-semibold text-foreground">This month&apos;s cost</h2>
              <p className="text-[24px] font-semibold text-foreground">
                {formatMoney(result.overview.costs.monthMicros)}
                <span className="ml-2 text-[13px] font-normal text-muted-foreground">
                  {result.overview.costs.capMicros !== null
                    ? `of ${formatMoney(result.overview.costs.capMicros)} cap`
                    : "no monthly cap set"}
                </span>
              </p>
              {Object.keys(result.overview.costs.byKind).length ? (
                <ul className="flex flex-wrap gap-2">
                  {Object.entries(result.overview.costs.byKind).map(([kind, micros]) => (
                    <li key={kind}>
                      <Chip>
                        {COST_LABELS[kind] ?? humanize(kind)}: {formatMoney(micros)}
                      </Chip>
                    </li>
                  ))}
                </ul>
              ) : null}
            </SoftCard>
          ) : null}
        </>
      )}
    </div>
  );
}
