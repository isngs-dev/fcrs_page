import Link from "next/link";
import { redirect } from "next/navigation";
import { ArrowRight, CalendarDays, CircleAlert, UserRound } from "lucide-react";
import { getClaims } from "@/lib/auth";
import { getDashboardPipeline, type PipelineColumn } from "@/lib/dashboard";
import { getBotSettings } from "@/lib/settings";
import {
  HUB_BUCKETS,
  HUB_PERIODS,
  getChatbotHub,
  type ChatbotHub,
  type HubBucket,
  type HubPeriod,
} from "@/lib/hub";
import { buildActivityRows, buildUsageBars } from "@/app/(protected)/hub-presentation";
import { relativeTime } from "@/app/(protected)/conversations/presentation";

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date unavailable";
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function LeadCard({ lead, featured }: { lead: PipelineColumn["items"][number]; featured: boolean }) {
  return (
    <article
      className={
        featured
          ? "flex flex-col gap-3 rounded-2xl bg-[#191a17] p-4 text-white shadow-[0_10px_26px_rgba(25,26,23,0.2)]"
          : "flex flex-col gap-3 rounded-2xl border border-[#e7e7e2] bg-white p-4"
      }
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-[15px] font-bold">{lead.name}</h3>
          <p className={featured ? "mt-1 truncate text-xs text-[#c6c7bd]" : "mt-1 truncate text-xs text-[#70716a]"}>
            {lead.source}
          </p>
        </div>
        <span
          className={
            featured
              ? "rounded-md bg-[#e4f222] px-1.5 py-0.5 text-[11px] font-bold text-[#191a17]"
              : "rounded-md bg-[#f2f2ec] px-1.5 py-0.5 text-[11px] font-semibold text-[#5a5b54]"
          }
        >
          {lead.status}
        </span>
      </div>
      <dl className={featured ? "grid gap-2 border-t border-white/15 pt-3 text-xs text-[#c6c7bd]" : "grid gap-2 border-t border-[#e7e7e2] pt-3 text-xs text-[#70716a]"}>
        <div className="flex items-center gap-2">
          <CalendarDays aria-hidden className="size-3.5" />
          <dt className="sr-only">Captured</dt>
          <dd>{formatDate(lead.createdAt)}</dd>
        </div>
        {lead.qualificationScore !== null ? (
          <div className="flex items-center gap-2">
            <UserRound aria-hidden className="size-3.5" />
            <dt className="sr-only">Qualification score</dt>
            <dd>Score {lead.qualificationScore}</dd>
          </div>
        ) : null}
        {lead.assignedAgentId ? (
          <div className="flex items-center gap-2">
            <UserRound aria-hidden className="size-3.5" />
            <dt className="sr-only">Assigned agent</dt>
            <dd className="truncate">Assigned</dd>
          </div>
        ) : null}
      </dl>
    </article>
  );
}

/**
 * Per-stage lead counts as paired bars, matching the 3a prototype's "New
 * leads" chart shape (dark #3d3e38 + citron #e4f222 per group). We have no
 * time-series metric to back a day-by-day chart, so each group is a pipeline
 * stage instead of a weekday: the dark bar is that stage's share of the
 * total pipeline, the citron bar is its share of the active (non-terminal)
 * leads. Both are derived from `getDashboardPipeline`'s real column totals
 * -- nothing here is fabricated.
 */
function StageDistributionChart({ columns }: { columns: PipelineColumn[] }) {
  const total = columns.reduce((sum, column) => sum + column.total, 0);
  const activeTotal = columns
    .filter((column) => column.key !== "converted")
    .reduce((sum, column) => sum + column.total, 0);
  const maxTotal = Math.max(...columns.map((column) => column.total), 1);

  return (
    <div>
      <p className="text-[13.5px] font-semibold text-[#c6c7bd]">Pipeline by stage</p>
      <div className="mt-4 flex items-end gap-3.5" style={{ height: 110 }}>
        {columns.map((column) => {
          const shareOfTotal = total > 0 ? (column.total / maxTotal) * 100 : 0;
          const shareOfActive =
            column.key !== "converted" && activeTotal > 0 ? (column.total / maxTotal) * 100 : 0;
          return (
            <div key={column.key} className="flex flex-col items-center gap-1.5">
              <div className="flex items-end gap-[3px]" style={{ height: 90 }}>
                <div
                  role="img"
                  aria-label={`${column.label}: ${column.total} leads`}
                  className="w-3 rounded-[3px] bg-[#3d3e38]"
                  style={{ height: `${Math.max(shareOfTotal, column.total > 0 ? 6 : 0)}%` }}
                />
                <div
                  className="w-3 rounded-[3px] bg-[#e4f222]"
                  style={{ height: `${Math.max(shareOfActive, shareOfActive > 0 ? 6 : 0)}%` }}
                />
              </div>
              <div className="text-[11px] text-[#9b9c93]">{column.label}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Half-donut built from a clipped conic-gradient ring, per HANDOFF-SPEC's 3a
 * recipe. Driven entirely by the real `qualificationRate` metric (already
 * computed in lib/dashboard.ts as progressed / total); renders a flat empty
 * ring rather than a fabricated percentage when the denominator is zero.
 */
function QualificationDonut({ rate }: { rate: number | null }) {
  const pct = rate === null ? 0 : Math.round(rate * 100);
  const sweepDeg = (pct / 100) * 180;

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative overflow-hidden" style={{ width: 170, height: 92 }}>
        <div
          className="absolute top-0 rounded-full"
          style={{
            width: 170,
            height: 170,
            background: `conic-gradient(from -90deg, #e4f222 0deg ${sweepDeg}deg, #3d3e38 ${sweepDeg}deg 180deg, transparent 180deg)`,
          }}
        />
        <div
          className="absolute rounded-full bg-[#191a17]"
          style={{ width: 126, height: 126, top: 22, left: 22 }}
        />
        <div className="absolute inset-x-0 bottom-0 text-center text-2xl leading-none font-bold text-white">
          {rate === null ? "–" : `${pct}%`}
        </div>
      </div>
      <div className="text-[13px] text-[#9b9c93]">Qualification rate</div>
    </div>
  );
}

function PipelineColumn({ column }: { column: PipelineColumn }) {
  const featured = column.key === "contacted";
  return (
    <section className="flex min-w-0 flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-heading text-xl font-semibold tracking-[-0.03em]">{column.label}</h2>
        <Link
          href={`/leads?stage=${column.key}`}
          className="rounded-lg border border-[#e7e7e2] px-2 py-1 text-xs text-[#5a5b54] transition-colors hover:bg-[#f7f7f3] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
        >
          {column.total} total
        </Link>
      </div>
      {column.items.length === 0 ? (
        <p role="status" className="rounded-2xl border border-dashed border-[#d8d9d0] bg-white/70 p-4 text-sm text-[#70716a]">
          No {column.label.toLowerCase()} leads yet.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {column.items.slice(0, 3).map((lead) => (
            <LeadCard key={lead.leadId} lead={lead} featured={featured} />
          ))}
        </div>
      )}
    </section>
  );
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function HubControls({ period, bucket }: { period: HubPeriod; bucket: HubBucket }) {
  return (
    <div className="flex flex-wrap gap-3">
      <form action="/" method="get" className="flex flex-wrap items-center gap-2" aria-label="Conversation usage period">
        <input type="hidden" name="bucket" value={bucket} />
        <fieldset className="flex overflow-hidden rounded-lg border border-[#e7e7e2] text-xs font-semibold">
          <legend className="sr-only">Conversation usage period</legend>
          {HUB_PERIODS.map((option) => (
            <label
              key={option}
              className="cursor-pointer px-2.5 py-1.5 capitalize text-[#5a5b54] transition-colors has-checked:bg-[#191a17] has-checked:text-white focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-[#191a17]"
            >
              <input type="radio" name="period" value={option} defaultChecked={period === option} className="sr-only" />
              {option}
            </label>
          ))}
        </fieldset>
        <button type="submit" className="min-h-8 rounded-lg border border-[#e7e7e2] bg-white px-2.5 text-xs font-semibold text-[#45463f] hover:bg-[#f7f7f3] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]">
          Apply
        </button>
      </form>

      <form action="/" method="get" className="flex flex-wrap items-center gap-2" aria-label="Responses chart bucket">
        <input type="hidden" name="period" value={period} />
        <fieldset className="flex overflow-hidden rounded-lg border border-[#e7e7e2] text-xs font-semibold">
          <legend className="sr-only">Responses chart bucket</legend>
          {HUB_BUCKETS.map((option) => (
            <label
              key={option}
              className="cursor-pointer px-2.5 py-1.5 capitalize text-[#5a5b54] transition-colors has-checked:bg-[#191a17] has-checked:text-white focus-within:outline-2 focus-within:outline-offset-[-2px] focus-within:outline-[#191a17]"
            >
              <input type="radio" name="bucket" value={option} defaultChecked={bucket === option} className="sr-only" />
              {option}
            </label>
          ))}
        </fieldset>
        <button type="submit" className="min-h-8 rounded-lg border border-[#e7e7e2] bg-white px-2.5 text-xs font-semibold text-[#45463f] hover:bg-[#f7f7f3] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]">
          Apply
        </button>
      </form>
    </div>
  );
}

function NewConversationsBars({ hub }: { hub: ChatbotHub }) {
  const series = hub.analytics.series.slice(-(hub.bucket === "day" ? 31 : 53));
  const max = Math.max(...series.map((entry) => entry.conversations), 1);
  return (
    <div className="mt-3 flex h-10 items-end gap-1" aria-label="New conversations over the selected period">
      {series.length === 0 ? <span className="text-xs text-[#70716a]">No activity yet</span> : null}
      {series.map((entry) => (
        <div
          key={entry.bucketStart}
          role="img"
          aria-label={`${entry.conversations} conversations`}
          className="min-w-1 flex-1 rounded-sm bg-[#191a17]"
          style={{ height: `${Math.max((entry.conversations / max) * 100, entry.conversations > 0 ? 10 : 0)}%` }}
        />
      ))}
    </div>
  );
}

/**
 * Half-ring gauge for "Successful Conversations" (SR-11 D2). Wired to the
 * REAL `deflectionRate` -- a `null` rate (empty window) renders the honest
 * "–" state, never a fabricated percentage. Monochrome per D6: a black
 * sweep on a light track, no citron. CSS-only conic-gradient, no new
 * client boundary (Constraint: prefer CSS-only for the gauge).
 */
/**
 * SR-12 D1: ~13 short radial tick marks evenly spaced across the 180deg arc,
 * like the mockup's speedometer dial. Pure decoration -- monochrome (#70716a
 * on the light track), CSS-only, no new data path and no new client boundary.
 * Each tick is an absolutely-positioned bar rotated about the gauge centre;
 * `GAUGE_TICKS` renders behind the arc/hub so the sweep still reads clearly.
 */
const GAUGE_TICK_COUNT = 13;
const GAUGE_TICKS = Array.from({ length: GAUGE_TICK_COUNT }, (_, index) => {
  // Evenly spread across the visible 180deg half-ring (0deg = left, 180deg =
  // right). -90 aligns the CSS rotation origin with the arc's -90deg start.
  const angle = (index / (GAUGE_TICK_COUNT - 1)) * 180 - 90;
  return angle;
});

function SuccessfulConversationsGauge({ rate }: { rate: number | null }) {
  const pct = rate === null ? 0 : Math.round(rate * 100);
  const sweepDeg = (pct / 100) * 180;
  return (
    // The arc is a full 128px circle whose CENTRE sits at the container's
    // bottom-centre (bottom:-64), so only its top half shows as the half-ring.
    // The radial ticks are anchored at that same bottom-centre point and
    // rotated about it, so they are concentric with the arc; each 72px arm
    // puts its 7px mark at radius ~65-72 -- just OUTSIDE the arc's 64px outer
    // edge, so the ticks stay visible around the dial.
    <div className="relative overflow-hidden" style={{ width: 148, height: 74 }} aria-hidden>
      {GAUGE_TICKS.map((angle) => (
        <div
          key={angle}
          className="absolute bottom-0 left-1/2 origin-bottom"
          style={{
            width: 2,
            height: 72,
            transform: `translateX(-50%) rotate(${angle}deg)`,
          }}
        >
          <div className="mx-auto rounded-full bg-[#70716a]" style={{ width: 2, height: 7 }} />
        </div>
      ))}
      <div
        className="absolute left-1/2 -translate-x-1/2 rounded-full"
        style={{
          width: 128,
          height: 128,
          bottom: -64,
          background: `conic-gradient(from -90deg, #191a17 0deg ${sweepDeg}deg, #e7e7e2 ${sweepDeg}deg 180deg, transparent 180deg)`,
        }}
      />
      <div
        className="absolute left-1/2 -translate-x-1/2 rounded-full bg-[#f0f0ea]"
        style={{ width: 94, height: 94, bottom: -47 }}
      />
    </div>
  );
}

/**
 * Small monochrome ring for the "Escalated to scheduling" card (SR-11 D5).
 * The label/subcopy stay verbatim honest -- this is presentation only. The
 * ring's fill is the escalate share of decided turns when derivable from
 * the real deflection rate (escalate share = 1 - deflectionRate); with no
 * real basis (deflectionRate null) it renders an empty ring, never a
 * fabricated share.
 */
function EscalatedRing({ escalated, deflectionRate }: { escalated: number; deflectionRate: number | null }) {
  const pct = deflectionRate === null ? null : Math.max(0, Math.round((1 - deflectionRate) * 100));
  const sweepDeg = pct === null ? 0 : (pct / 100) * 360;
  return (
    <div className="relative shrink-0" style={{ width: 56, height: 56 }} aria-hidden>
      <div
        className="absolute inset-0 rounded-full"
        style={{ background: `conic-gradient(#191a17 0deg ${sweepDeg}deg, #e7e7e2 ${sweepDeg}deg 360deg)` }}
      />
      <div className="absolute rounded-full bg-white" style={{ width: 40, height: 40, top: 8, left: 8 }} />
      <div className="absolute inset-0 flex items-center justify-center text-[9px] font-bold text-[#191a17]">
        {escalated > 0 ? escalated : ""}
      </div>
    </div>
  );
}

function ResponsesChart({ hub }: { hub: ChatbotHub }) {
  const series = hub.analytics.series.slice(-(hub.bucket === "day" ? 31 : 53));
  const max = Math.max(...series.flatMap((entry) => [entry.answers, entry.escalations]), 1);
  return (
    <section className="rounded-2xl border border-[#e7e7e2] bg-white p-5" aria-label="Responses over time">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-bold text-[#191a17]">Responses over time</h2>
          <p className="mt-1 text-xs text-[#70716a]">Answered by chatbot and escalated to scheduling.</p>
        </div>
        <div className="flex items-center gap-3 text-xs text-[#70716a]">
          <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-sm bg-[#191a17]" />Answered</span>
          <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-sm border border-[#9b9c93] bg-[repeating-linear-gradient(45deg,#d5d5cb,#d5d5cb_2px,#9b9c93_2px,#9b9c93_4px)]" />Escalated</span>
        </div>
      </div>
      {series.length === 0 ? (
        <p role="status" className="mt-5 rounded-lg border border-dashed border-[#d8d9d0] bg-[#f7f7f3] p-4 text-sm text-[#70716a]">
          No conversations yet in this period.
        </p>
      ) : (
        <div className="mt-5 flex h-60 items-end gap-1.5 overflow-x-hidden" aria-label="Answered and escalated response bars">
          {series.map((entry, index) => (
            <div
              key={entry.bucketStart}
              tabIndex={0}
              aria-describedby={`responses-tooltip-${index}`}
              className="group relative flex min-w-2 flex-1 items-end justify-center gap-0.5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
              style={{ height: 220 }}
            >
              <div
                id={`responses-tooltip-${index}`}
                role="tooltip"
                className={`pointer-events-none absolute bottom-[calc(100%+0.5rem)] z-10 w-max max-w-[calc(100vw-2rem)] rounded-lg border border-[#30312d] bg-[#191a17] p-2 text-xs text-white shadow-sm invisible opacity-0 transition-opacity group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100 ${
                  index === 0 ? "left-0 translate-x-0" : index === series.length - 1 ? "right-0 translate-x-0" : "left-1/2 -translate-x-1/2"
                }`}
              >
                <p className="font-semibold">{formatDate(entry.bucketStart)}</p>
                <p className="mt-1">Answered: {entry.answers}</p>
                <p>Escalated: {entry.escalations}</p>
              </div>
              <div
                role="img"
                aria-label={`${entry.answers} answered by chatbot`}
                className="w-full max-w-3 rounded-t-sm bg-[#191a17]"
                style={{ height: `${Math.max((entry.answers / max) * 100, entry.answers > 0 ? 4 : 0)}%` }}
              />
              <div
                role="img"
                aria-label={`${entry.escalations} escalated to scheduling`}
                className="w-full max-w-3 rounded-t-sm border border-[#9b9c93] bg-[repeating-linear-gradient(45deg,#d5d5cb,#d5d5cb_2px,#9b9c93_2px,#9b9c93_4px)]"
                style={{ height: `${Math.max((entry.escalations / max) * 100, entry.escalations > 0 ? 4 : 0)}%` }}
              />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function ChatbotHubSection({ hub }: { hub: ChatbotHub }) {
  const { analytics, activeConversations, closedConversations, period } = hub;
  const successful = analytics.deflectionRate === null ? "–" : `${Math.round(analytics.deflectionRate * 100)}%`;
  const answered = analytics.decisionDistribution.answer ?? 0;
  const escalated = analytics.decisionDistribution.escalate ?? 0;
  const activityRows = buildActivityRows(activeConversations.items, formatDate);
  const usageBars = buildUsageBars(period, analytics.totals.conversations, HUB_PERIODS);

  return (
    <section aria-label="Chatbot hub" className="mt-6 flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold tracking-[0.08em] text-[#96978e] uppercase">Chatbot hub</p>
          <h2 className="mt-1 font-heading text-xl font-bold tracking-[-0.035em]">Conversation activity</h2>
        </div>
        <HubControls period={hub.period} bucket={hub.bucket} />
      </div>

      <div className="rounded-3xl bg-[#f0f0ea] p-6 sm:p-8">
        <div className="grid gap-8 sm:grid-cols-2 sm:divide-x sm:divide-[#e0e0d8] lg:grid-cols-4 lg:gap-0">
          <div className="sm:pr-8">
            <p className="text-sm font-semibold text-[#45463f]">New Conversations</p>
            <p className="mt-3 text-4xl font-bold tracking-[-0.05em] tabular-nums text-[#191a17]">
              {analytics.totals.conversations}
            </p>
            <NewConversationsBars hub={hub} />
          </div>
          <div className="sm:pl-8 lg:px-8">
            <p className="text-sm font-semibold text-[#45463f]">Successful Conversations</p>
            <div className="mt-3 flex items-end gap-3">
              <SuccessfulConversationsGauge rate={analytics.deflectionRate} />
              <p className="pb-2 text-4xl font-bold tracking-[-0.05em] tabular-nums text-[#191a17]">{successful}</p>
            </div>
            <p className="mt-2 text-xs text-[#70716a]">Deflection rate — handled without escalation to scheduling.</p>
          </div>
          <Link
            href="/conversations?status=active"
            className="group sm:pr-8 lg:border-l lg:border-[#e0e0d8] lg:pl-8 lg:pr-8 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17] rounded-lg"
          >
            <p className="flex items-center justify-between text-sm font-semibold text-[#45463f]">
              Active Conversations
              <ArrowRight aria-hidden className="size-4 text-[#96978e] transition-transform group-hover:translate-x-0.5" />
            </p>
            <p className="mt-3 text-4xl font-bold tracking-[-0.05em] tabular-nums text-[#191a17]">
              {activeConversations.total}
            </p>
            <p className="mt-2 text-xs text-[#70716a]">Currently open conversations.</p>
          </Link>
          <Link
            href="/conversations?status=ended"
            className="group sm:pl-8 lg:border-l lg:border-[#e0e0d8] lg:pl-8 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17] rounded-lg"
          >
            <p className="flex items-center justify-between text-sm font-semibold text-[#45463f]">
              Closed Conversations
              <ArrowRight aria-hidden className="size-4 text-[#96978e] transition-transform group-hover:translate-x-0.5" />
            </p>
            <p className="mt-3 text-4xl font-bold tracking-[-0.05em] tabular-nums text-[#191a17]">
              {closedConversations.total}
            </p>
            <p className="mt-2 text-xs text-[#70716a]">Conversations that have ended.</p>
          </Link>
        </div>
      </div>

      {analytics.totals.conversations === 0 ? (
        <p role="status" className="rounded-2xl border border-dashed border-[#d8d9d0] bg-[#f7f7f3] p-4 text-sm text-[#70716a]">
          No conversations yet in this period.
        </p>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-3">
        <article className="rounded-2xl border border-[#e7e7e2] bg-white p-6">
          <p className="text-sm font-semibold text-[#45463f]">Conversation usage</p>
          <p className="mt-3 text-4xl font-bold tracking-[-0.05em] tabular-nums text-[#191a17]">{analytics.totals.conversations}</p>
          <p className="mt-2 text-xs text-[#70716a]">Conversations this {period}. This is usage only — no limit is enforced.</p>
          {/* SR-12 D2: presence bars for the single, real selected-period total
              (only that period is fetched -- we never fabricate week/month/year
              numbers). The other periods render as empty tracks and the label
              row echoes the existing single `period` selection from HubControls. */}
          <div className="mt-4 flex flex-col gap-2" aria-hidden>
            {usageBars.map((bar) => (
              <div key={bar.period} className="h-2.5 overflow-hidden rounded-full bg-[#f0f0ea]">
                {bar.fill > 0 ? (
                  <div
                    className={bar.selected ? "h-full rounded-full bg-[#191a17]" : "h-full rounded-full bg-[#c6c7bd]"}
                    style={{ width: `${bar.fill * 100}%` }}
                  />
                ) : null}
              </div>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] font-medium">
            {usageBars.map((bar) => (
              <span
                key={bar.period}
                className={
                  bar.selected
                    ? "inline-flex items-center gap-1.5 capitalize text-[#191a17]"
                    : "inline-flex items-center gap-1.5 capitalize text-[#96978e]"
                }
              >
                <span
                  className={bar.selected ? "size-1.5 rounded-full bg-[#191a17]" : "size-1.5 rounded-full bg-[#c6c7bd]"}
                  aria-hidden
                />
                {bar.period}
              </span>
            ))}
          </div>
        </article>
        <article className="rounded-2xl border border-[#e7e7e2] bg-white p-6">
          <p className="text-sm font-semibold text-[#45463f]">Escalated to scheduling</p>
          <div className="mt-3 flex items-end gap-3">
            <EscalatedRing escalated={escalated} deflectionRate={analytics.deflectionRate} />
            <p className="pb-1 text-4xl font-bold tracking-[-0.05em] tabular-nums text-[#191a17]">{escalated}</p>
          </div>
          <p className="mt-2 text-xs text-[#70716a]">The bot offered a booking flow; this is not a live-operator transfer.</p>
        </article>
        <article className="rounded-2xl border border-[#e7e7e2] bg-white p-6">
          <div className="flex items-start justify-between gap-3">
            <p className="text-sm font-semibold text-[#45463f]">Answered by chatbot</p>
            <Link
              href="/conversations?status=active"
              className="rounded text-xs font-semibold text-[#5a5b54] underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17]"
            >
              See all
            </Link>
          </div>
          <p className="mt-3 text-4xl font-bold tracking-[-0.05em] tabular-nums text-[#191a17]">{answered}</p>
          {/* SR-12 D3: up to 3 honest activity rows from the real active
              conversations. Each row's avatar/label is derived from the real
              `conversationId` (never a fabricated visitor name -- widget
              visitors are anonymous), with the real `summary` (falling back to
              `Started <date>` as the prior single-row preview did) and the real
              `status` + relative time. */}
          {activityRows.length > 0 ? (
            <ul className="mt-3 flex flex-col divide-y divide-[#e7e7e2] border-t border-[#e7e7e2]">
              {activityRows.map((row) => (
                <li key={row.conversationId} className="flex items-center gap-3 py-3">
                  <span
                    aria-hidden
                    className="flex size-8 shrink-0 items-center justify-center rounded-full bg-[#f0f0ea] text-[11px] font-bold text-[#45463f]"
                  >
                    {row.initials}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-semibold text-[#45463f]">{row.identityLabel}</p>
                    <p className="truncate text-xs text-[#70716a]">{row.preview}</p>
                  </div>
                  <span className="shrink-0 rounded-md bg-[#f2f2ec] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[#5a5b54]">
                    {row.status === "active" ? relativeTime(row.startedAt) : row.status}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-3 text-xs text-[#70716a]">No active conversation to preview.</p>
          )}
        </article>
      </div>

      <ResponsesChart hub={hub} />
    </section>
  );
}

interface ProtectedHomePageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

export default async function ProtectedHomePage({ searchParams }: ProtectedHomePageProps) {
  const claims = await getClaims();
  if (!claims) {
    redirect("/login");
  }
  if (claims.role === "PLATFORM_ADMIN") redirect("/clients");
  if (claims.role !== "CLIENT_ADMIN" && claims.role !== "CLIENT_AGENT") redirect("/login");

  const params = await searchParams;
  const period = firstValue(params.period);
  const bucket = firstValue(params.bucket);
  const [result, settingsResult, hubResult] = await Promise.all([
    getDashboardPipeline(),
    getBotSettings(),
    getChatbotHub({ period, bucket }),
  ]);
  const dashboardTitle =
    settingsResult.status === "ok" && settingsResult.settings.dashboardTitle?.trim()
      ? settingsResult.settings.dashboardTitle
      : "Dashboard";

  return (
    <main className="flex flex-1 flex-col px-4 py-8 sm:px-6 lg:px-7 lg:py-10">
      <header className="flex flex-col gap-4 border-b border-[#e7e7e2] pb-5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-xs font-semibold tracking-[0.08em] text-[#96978e] uppercase">Lead workspace</p>
          <h1 className="mt-1 font-heading text-2xl font-bold tracking-[-0.04em] sm:text-[28px]">{dashboardTitle}</h1>
        </div>
        <Link
          href="/conversations"
          className="inline-flex w-fit self-start min-h-11 items-center justify-center gap-2 rounded-xl bg-[#191a17] px-3.5 text-sm font-semibold text-white transition-colors hover:bg-[#30312d] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#191a17] sm:self-auto"
        >
          Review Conversations
          <ArrowRight aria-hidden className="size-4" />
        </Link>
      </header>

      {hubResult.status === "error" ? (
        <div role="alert" className="mt-6 rounded-2xl border border-[#c2452d]/30 bg-[#fff3ee] p-4 text-sm text-[#8f2e1c]">
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
        <ChatbotHubSection hub={hubResult.data} />
      )}

      {result.status === "error" ? (
        <div role="alert" className="mt-6 rounded-2xl border border-[#c2452d]/30 bg-[#fff3ee] p-4 text-sm text-[#8f2e1c]">
          <div className="flex gap-2">
            <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
            <div>
              <p className="font-semibold">The lead pipeline could not be loaded.</p>
              <p className="mt-1">{result.message}</p>
              {result.correlationId ? <p className="mt-1 text-xs">Correlation ID: {result.correlationId}</p> : null}
            </div>
          </div>
        </div>
      ) : (
        <>
          <section
            aria-label="Lead pipeline overview"
            className="mt-6 grid gap-8 rounded-2xl bg-[#191a17] p-5 text-white sm:p-7 lg:grid-cols-[1.2fr_1fr_0.9fr_0.9fr] lg:items-center"
          >
            <StageDistributionChart columns={result.columns} />
            <QualificationDonut rate={result.metrics.qualificationRate} />
            <div>
              <p className="text-[13.5px] font-semibold text-[#c6c7bd]">Pipeline leads</p>
              <p className="mt-2.5 text-4xl leading-none font-bold tracking-[-0.05em] tabular-nums text-white">
                {result.metrics.total}
              </p>
              <p className="mt-2.5 text-[13.5px] leading-[1.4] text-[#9b9c93]">
                Across the four
                <br />
                active stages
              </p>
            </div>
            <div>
              <p className="text-[13.5px] font-semibold text-[#c6c7bd]">Converted</p>
              <p className="mt-2.5 text-4xl leading-none font-bold tracking-[-0.05em] tabular-nums text-[#e4f222]">
                {result.metrics.converted}
              </p>
              <p className="mt-2.5 text-[13.5px] leading-[1.4] text-[#9b9c93]">
                Current converted
                <br />
                stage
              </p>
            </div>
          </section>

          <section aria-label="Lead pipeline" className="mt-7 grid gap-7 md:grid-cols-2 2xl:grid-cols-4">
            {result.columns.map((column) => <PipelineColumn key={column.key} column={column} />)}
          </section>
        </>
      )}
    </main>
  );
}
