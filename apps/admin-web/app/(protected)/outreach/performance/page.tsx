/**
 * Performance (A13 R2/R3): how outreach is doing over a period, and the past meetings
 * still waiting for "did it happen?". Agents see the same numbers without cost.
 */
import { requireAnyRole } from "@/lib/auth";
import { getPerformance, setMeetingOutcome } from "@/lib/outreach";
import { formatMoney, formatWhen, percent } from "@/lib/outreach-format";
import { SoftCard } from "@/components/admin/soft-card";
import { ErrorCard, FilterPills, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";
import { ActionButton } from "@/app/(protected)/outreach/action-button";

type SearchParams = Promise<{ [key: string]: string | string[] | undefined }>;
const PERIODS = [7, 30, 90];

function Metric({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <SoftCard className="flex flex-col gap-1 px-5 py-4">
      <span className="text-[12px] font-medium text-muted-foreground">{label}</span>
      <span className="text-[24px] font-semibold text-foreground">{value}</span>
      {hint ? <span className="text-[11.5px] text-muted-foreground">{hint}</span> : null}
    </SoftCard>
  );
}

export default async function PerformancePage({ searchParams }: { searchParams: SearchParams }) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");
  const raw = (await searchParams).days;
  const asked = Number(Array.isArray(raw) ? raw[0] : raw);
  const days = PERIODS.includes(asked) ? asked : 30;
  const result = await getPerformance(days);

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Performance" subtitle="How Rebecca's follow-up is doing." />
      <FilterPills
        active={String(days)}
        pills={PERIODS.map((d) => ({ label: `Last ${d} days`, value: String(d), href: `/outreach/performance?days=${d}` }))}
      />
      {result.status === "error" ? (
        <ErrorCard message={result.message} correlationId={result.correlationId} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Metric label="New leads" value={result.performance.leads} hint={`${result.performance.contacted} contacted`} />
            <Metric
              label="Time to first contact"
              value={result.performance.medianResponseMinutes === null ? "—" : `${result.performance.medianResponseMinutes} min`}
              hint="median"
            />
            <Metric label="Reply rate" value={percent(result.performance.replyRate)} hint={`${result.performance.emailsSent} sent`} />
            <Metric label="Lead → meeting" value={percent(result.performance.leadToMeeting)} hint={`${result.performance.meetings} meetings`} />
            <Metric
              label="Show rate"
              value={percent(result.performance.showRate)}
              hint={`${result.performance.attended} attended · ${result.performance.noShow} no-show`}
            />
            <Metric label="Call connection" value={percent(result.performance.callConnectionRate)} hint={`${result.performance.callsPlaced} calls`} />
            <Metric label="Bounce rate" value={percent(result.performance.bounceRate)} />
            <Metric
              label="Opt-out rate"
              value={percent(result.performance.optOutRate)}
              hint={`complaints ${percent(result.performance.complaintRate)}`}
            />
          </div>
          {result.performance.costs ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Metric label="Spend" value={formatMoney(result.performance.costs.totalMicros)} />
              <Metric
                label="Cost per lead"
                value={result.performance.costs.perLeadMicros === null ? "—" : formatMoney(result.performance.costs.perLeadMicros)}
              />
              <Metric
                label="Cost per meeting"
                value={result.performance.costs.perMeetingMicros === null ? "—" : formatMoney(result.performance.costs.perMeetingMicros)}
              />
            </div>
          ) : null}
          <SoftCard className="px-5 py-4">
            <h2 className="pb-1 text-[16px] font-semibold">Did these meetings happen?</h2>
            <p className="pb-2 text-[12.5px] text-muted-foreground">Marking them keeps the show rate honest.</p>
            {result.performance.meetingsToMark.length === 0 ? (
              <p className="text-[12.5px] text-muted-foreground">Nothing to mark.</p>
            ) : (
              <ul>
                {result.performance.meetingsToMark.map((m) => (
                  <li key={m.eventId} className="flex flex-wrap items-center gap-3 border-b border-[var(--row-line)] py-2 last:border-b-0">
                    <span className="min-w-0 flex-1 text-[13px]">
                      <span className="font-semibold">{m.name ?? m.email ?? "Meeting"}</span>
                      <span className="text-muted-foreground"> · {formatWhen(m.startsAt)}</span>
                    </span>
                    <ActionButton action={setMeetingOutcome.bind(null, m.eventId, "attended")} label="Attended" variant="outline" />
                    <ActionButton action={setMeetingOutcome.bind(null, m.eventId, "no_show")} label="No-show" variant="ghost" />
                  </li>
                ))}
              </ul>
            )}
          </SoftCard>
        </>
      )}
    </div>
  );
}
