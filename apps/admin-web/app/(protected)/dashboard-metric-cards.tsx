/**
 * SR-16 scope item 4 -- the three metric `SoftCard`s (M6, D2/D3/D4/D5):
 * Conversation Rate (deflectionRate, D3), Escalation Rate (series-derived
 * donut, D4), Answered by chatbot (real conversation rows, D5 -- no
 * fabricated visitor names). All URL-driven; no client state.
 */
import Link from "next/link";
import { SoftCard } from "@/components/admin/soft-card";
import { HUB_PERIODS, type ChatbotHub, type HubPeriod } from "@/lib/hub";
import { formatRate } from "@/lib/analytics";
import {
  answeredByChatbotRate,
  buildActivityRows,
  buildEscalationReading,
} from "@/app/(protected)/hub-presentation";
import { relativeTime } from "@/app/(protected)/conversations/presentation";

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date unavailable";
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/**
 * D3: this card renders the SAME real metric as the hero gauge
 * (`deflectionRate`, via `answeredByChatbotRate`) -- deliberately, per D3's
 * rationale (one honest label used twice beats one honest and one
 * invented). The Week/Month/Year selector maps 1:1 to `HUB_PERIODS`.
 */
function ConversationRateCard({ hub }: { hub: ChatbotHub }) {
  const rate = answeredByChatbotRate(hub.analytics);
  const stages = HUB_PERIODS.map((period) => ({
    period,
    // Only the selected period has a real fetched total (single-read
    // design, matches buildUsageBars' honesty rule) -- non-selected periods
    // render an inert/empty bar, never a fabricated value.
    selected: period === hub.period,
  }));

  return (
    <SoftCard as="article" className="flex w-full flex-col gap-1.5 px-4 py-3.5">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-base font-semibold text-[var(--ink)]">Conversation Rate</h3>
        <PeriodSelector bucket={hub.bucket} period={hub.period} />
      </div>
      <p className="text-xs text-muted-foreground">
        {formatRate(rate)} answered by chatbot, this {hub.period}.
      </p>

      {/* Reference Dashboard.dc.html:208-212 -- three stacked 9px segmented
          tracks (radius 5): Week solid near-black, Month a 45deg
          #878787/#cdcdcd hatch, Year solid #cdcdcd. The shipped version drew
          three VERTICAL bars instead, which is the geometry gap SR-23 flags.
          Only the selected period carries a real fetched total (single-read
          design), so the non-selected tracks render as their reference
          pattern at reduced opacity rather than as fabricated fills. */}
      <div aria-hidden className="mt-auto flex flex-col gap-[7px] pt-4">
        {stages.map((stage) => (
          <div
            key={stage.period}
            className="h-[9px] rounded-[5px]"
            style={{
              background: SEGMENT_BACKGROUNDS[stage.period],
              opacity: stage.selected ? 1 : 0.45,
            }}
          />
        ))}
      </div>
      <div className="mt-2 flex gap-2.5 text-[10.5px] text-[var(--ink-2)]">
        {stages.map((stage) => (
          <span key={stage.period} className="flex items-center gap-[5px] capitalize">
            <span
              aria-hidden
              className="size-[7px] rounded-full"
              style={{ background: SEGMENT_DOTS[stage.period] }}
            />
            {stage.period}
          </span>
        ))}
      </div>
    </SoftCard>
  );
}

/** Reference Dashboard.dc.html:209-211 / :204-206 -- the three segment fills
 * and their matching legend dots, keyed by the `HUB_PERIODS` they map onto. */
const SEGMENT_BACKGROUNDS: Record<HubPeriod, string> = {
  week: "var(--primary)",
  month: "repeating-linear-gradient(45deg,#878787 0 4px,#cdcdcd 4px 8px)",
  year: "#cdcdcd",
};

const SEGMENT_DOTS: Record<HubPeriod, string> = {
  week: "var(--primary)",
  month: "#878787",
  year: "#cdcdcd",
};

function PeriodSelector({ period, bucket }: { period: HubPeriod; bucket: string }) {
  return (
    <form action="/" method="get" aria-label="Conversation rate period">
      <input type="hidden" name="bucket" value={bucket} />
      <select
        name="period"
        defaultValue={period}
        className="rounded-md border border-border bg-card px-1.5 py-1 text-[11px] font-semibold text-[var(--ink-2)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        // Progressive enhancement: submits on change; the Apply button below
        // covers the no-JS / keyboard-without-change path.
      >
        {HUB_PERIODS.map((option) => (
          <option key={option} value={option}>
            {option[0]!.toUpperCase() + option.slice(1)}
          </option>
        ))}
      </select>
      <button
        type="submit"
        className="ml-1.5 rounded-md border border-border bg-card px-1.5 py-1 text-[11px] font-semibold text-[var(--ink-2)] hover:bg-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        Apply
      </button>
    </form>
  );
}

/** SVG donut, `stroke-dasharray` technique (D6/M9, matching the mockup's
 * own recipe at `Console.dc.html:216-219`). `null` renders an EMPTY track
 * with no filled arc -- never a 0%-labelled ring that looks like a real
 * zero-value reading (M4). */
function EscalationDonut({ reading }: { reading: ReturnType<typeof buildEscalationReading> }) {
  // Reference Dashboard.dc.html:216-219 -- 56x56 box, r=23, stroke-width 7,
  // track #cdcdcd, filled arc near-black with a round cap, whole SVG rotated
  // -90deg so the arc starts at 12 o'clock. The artboard hardcodes
  // `stroke-dasharray:144.5` (== 2*PI*23) with a dashoffset; we compute the
  // same circumference and express the fill as a dasharray pair, which is
  // equivalent and avoids a magic number drifting from the radius.
  const radius = 23;
  const circumference = 2 * Math.PI * radius;
  const filled = reading.percent === null ? 0 : (reading.percent / 100) * circumference;

  return (
    <svg width={56} height={56} viewBox="0 0 56 56" className="absolute inset-0 -rotate-90" aria-hidden>
      <circle cx={28} cy={28} r={radius} fill="none" stroke="#cdcdcd" strokeWidth={7} />
      {reading.percent !== null ? (
        <circle
          cx={28}
          cy={28}
          r={radius}
          fill="none"
          stroke="var(--primary)"
          strokeWidth={7}
          strokeDasharray={`${filled} ${circumference - filled}`}
          strokeLinecap="round"
        />
      ) : null}
    </svg>
  );
}

function EscalationRateCard({ hub }: { hub: ChatbotHub }) {
  const reading = buildEscalationReading(hub.analytics);

  return (
    <SoftCard as="article" aria-label={reading.label} className="flex w-full flex-col gap-3 px-[18px] py-4">
      <h3 className="text-base leading-tight font-semibold text-[var(--ink)]">Escalation Rate</h3>
      {/* Reference Dashboard.dc.html:214-226 -- ring on the left with the
          percentage centred INSIDE it, two 5px-dot legend rows on the right
          (the shipped version put a 3xl number beside the ring instead). */}
      <div className="flex items-center gap-3.5">
        <div role="img" aria-label={reading.label} className="relative size-14 flex-none">
          <EscalationDonut reading={reading} />
          <div className="absolute inset-0 flex items-center justify-center text-center text-[13px] font-semibold text-[var(--ink)]">
            {reading.percent === null ? (
              <span className="text-[9px] text-[var(--ink-2)]">No data</span>
            ) : (
              `${reading.percent}%`
            )}
          </div>
        </div>
        <div className="flex min-w-0 flex-col gap-2.5 text-[12.5px] text-[var(--ink-2)]">
          <span className="flex items-center gap-2">
            <span aria-hidden className="size-[5px] flex-none rounded-full bg-primary" />
            {reading.escalations} escalated
          </span>
          <span className="flex items-center gap-2">
            <span aria-hidden className="size-[5px] flex-none rounded-full bg-primary" />
            {reading.percent === null
              ? "No conversations in this period"
              : `${100 - reading.percent}% resolved`}
          </span>
        </div>
      </div>
      <table className="sr-only">
        <caption>Escalation rate</caption>
        <tbody>
          <tr>
            <th scope="row">Rate</th>
            <td>{reading.percent === null ? "No data" : `${reading.percent}%`}</td>
          </tr>
          <tr>
            <th scope="row">Escalated</th>
            <td>{reading.escalations}</td>
          </tr>
          <tr>
            <th scope="row">Handled without escalation</th>
            <td>{reading.handled}</td>
          </tr>
        </tbody>
      </table>
    </SoftCard>
  );
}

/**
 * D5: real conversations only -- `conversationShortId`/`buildActivityRows`,
 * never a fabricated visitor name. An empty `items` list renders an honest
 * empty state, not sample rows.
 */
function AnsweredByChatbotCard({ hub }: { hub: ChatbotHub }) {
  const rows = buildActivityRows(hub.activeConversations.items, formatDate);

  return (
    <SoftCard as="article" className="flex w-full flex-col px-[18px] py-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-base font-semibold text-[var(--ink)]">Answered by chatbot</h3>
        <Link
          href="/conversations"
          className="rounded text-xs font-semibold text-[var(--ink-2)] underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          See all
        </Link>
      </div>
      {rows.length === 0 ? (
        <p role="status" className="mt-2 text-xs text-muted-foreground">
          No active conversations to show yet.
        </p>
      ) : (
        /* Reference Dashboard.dc.html:235-245 -- 26px grey avatar, a 12px/600
           name over an 11px muted preview, and a cream rounded-full time pill
           (the shipped rows used a 32px avatar and a square uppercase chip). */
        <ul className="mt-2 flex flex-1 flex-col justify-center gap-1.5">
          {rows.map((row) => (
            <li key={row.conversationId} className="flex items-center gap-2.5">
              <span
                aria-hidden
                className="flex size-[26px] flex-none items-center justify-center rounded-full bg-border text-[10px] font-semibold text-[var(--ink-2)]"
              >
                {row.initials}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs leading-[1.25] font-semibold text-[var(--ink)]">
                  {row.identityLabel}
                </p>
                <p className="truncate text-[11px] leading-[1.25] text-muted-foreground">{row.preview}</p>
              </div>
              <span className="flex-none rounded-full bg-secondary px-[9px] py-[3px] text-[10.5px] font-semibold text-[var(--ink-2)]">
                {row.status === "active" ? relativeTime(row.startedAt) : row.status}
              </span>
            </li>
          ))}
        </ul>
      )}
    </SoftCard>
  );
}

export function DashboardMetricCards({ hub }: { hub: ChatbotHub }) {
  // Reference Dashboard.dc.html:193 -- one 16px-gap row of three cards at
  // flex 1 / 1 / 1.2 (the activity list is the wider one). Stacks below `lg:`.
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:flex lg:flex-row lg:items-stretch">
      <div className="flex min-w-0 lg:flex-1">
        <ConversationRateCard hub={hub} />
      </div>
      <div className="flex min-w-0 lg:flex-1">
        <EscalationRateCard hub={hub} />
      </div>
      <div className="flex min-w-0 sm:col-span-2 lg:flex-[1.2]">
        <AnsweredByChatbotCard hub={hub} />
      </div>
    </div>
  );
}

