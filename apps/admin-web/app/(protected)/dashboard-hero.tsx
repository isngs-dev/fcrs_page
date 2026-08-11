/**
 * SR-16 scope item 2 -- the dashboard's cream hero strip (M6, D2, D8). A
 * `--secondary` panel holding the kicker/title/actions, the "New
 * Conversations" weekly bars (`series[].conversations`), the "Answered by
 * chatbot" gauge (`deflectionRate`, D3 -- NOT literally "Conversation
 * rate", see `buildGaugeReading`), and the Active/Closed conversation stat
 * readouts. Composed entirely from SR-15 tokens -- no new palette here.
 *
 * D8: flex/grid that reflows on mobile (a column that becomes a responsive
 * grid at `sm:`), NOT the mockup's absolute pixel offsets. `--line-2`
 * dividers only render at `sm:` and up (a stacked mobile layout has no
 * meaningful vertical divider to draw).
 */
import Link from "next/link";
import { ArrowRight, Filter } from "lucide-react";
import type { ChatbotHub } from "@/lib/hub";
import { buildGaugeReading } from "@/app/(protected)/hub-presentation";

function WeeklyConversationBars({ hub }: { hub: ChatbotHub }) {
  const series = hub.analytics.series.slice(-7);
  const max = Math.max(...series.map((entry) => entry.conversations), 1);
  const summary = series
    .map((entry) => `${formatDayLabel(entry.bucketStart)}: ${entry.conversations}`)
    .join(", ");

  if (series.length === 0) {
    return <p className="mt-3 text-xs text-[var(--ink-2)]">No conversations yet in this period.</p>;
  }

  return (
    <>
      {/* Reference Dashboard.dc.html:104-111 -- a 78px-tall BAR track, bars
          capped at 15px wide with a 9px gutter; the day label sits BELOW that
          track (its own row), not inside it -- reserving the bar's full
          height for the bar itself is what stops a near-max bar from pushing
          its label (or the "New Conversations" title above the track) out of
          place. */}
      <div
        role="img"
        aria-label={`New conversations by day: ${summary}`}
        className="mt-3 flex gap-[9px]"
      >
        {series.map((entry) => (
          <div key={entry.bucketStart} className="flex flex-1 flex-col items-center gap-[7px]">
            <div className="flex h-[78px] w-full items-end justify-center">
              <div
                className="w-full max-w-[15px] rounded-[3px] bg-primary"
                style={{ height: `${(entry.conversations / max) * 78}px` }}
              />
            </div>
            <span className="text-[10.5px] text-muted-foreground">
              {formatDayLabel(entry.bucketStart)}
            </span>
          </div>
        ))}
      </div>
      <table className="sr-only">
        <caption>New conversations by day</caption>
        <thead>
          <tr>
            <th scope="col">Day</th>
            <th scope="col">Conversations</th>
          </tr>
        </thead>
        <tbody>
          {series.map((entry) => (
            <tr key={entry.bucketStart}>
              <td>{formatDayLabel(entry.bucketStart)}</td>
              <td>{entry.conversations}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function formatDayLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { weekday: "short" });
}

/**
 * Reference gauge math, read verbatim from `Dashboard.dc.html`'s
 * `renderVals()` (lines 261-266): 26 ticks sweeping -80deg..+80deg in equal
 * `160/(26-1)` steps. A tick is fully opaque when its index falls below
 * `round(26 * rate)` and drops to 0.2 opacity otherwise -- the fill is
 * expressed purely by tick opacity, NOT by an arc/sweep/needle (the
 * conic-gradient sweep + cream masking disc that shipped before were an
 * invention; the reference draws no arc at all).
 */
const GAUGE_TICK_COUNT = 26;
const GAUGE_TICK_ANGLES = Array.from(
  { length: GAUGE_TICK_COUNT },
  (_, index) => -80 + (160 / (GAUGE_TICK_COUNT - 1)) * index
);

/** Exported for unit test: how many of the 26 ticks light up for a rate.
 * A `null` rate lights ZERO ticks, which is visually distinct from a real
 * 0% only in combination with the "No data" centre label (M4). */
export function activeGaugeTickCount(rate: number | null, total = GAUGE_TICK_COUNT): number {
  if (rate === null) return 0;
  return Math.round(total * rate);
}

/**
 * The hero's tick-based gauge (reference `Dashboard.dc.html:116-124`: a
 * 112x60 box, ticks `2x10px` rotated about `bottom center` and pushed out by
 * `translateY(-47px)`, with the 21px/600 percentage sitting at `bottom:-2px`
 * and a 12.5px caption under it). Renders `deflectionRate` via
 * `buildGaugeReading` (D3) -- when the reading is `null` every tick renders
 * dimmed and the centre reads "No data", never a 0% fill (M4).
 */
export function AnsweredByChatbotGauge({ hub }: { hub: ChatbotHub }) {
  const reading = buildGaugeReading(hub.analytics);
  const activeTicks = activeGaugeTickCount(reading.rate);

  return (
    <div className="flex flex-col items-center">
      <div role="img" aria-label={reading.label} className="relative" style={{ width: 112, height: 60 }}>
        {GAUGE_TICK_ANGLES.map((angle, index) => (
          <div
            key={angle}
            aria-hidden
            className="absolute bottom-0 left-1/2 rounded-[2px] bg-primary"
            style={{
              width: 2,
              height: 10,
              transformOrigin: "bottom center",
              transform: `translateX(-50%) rotate(${angle}deg) translateY(-47px)`,
              opacity: index < activeTicks ? 1 : 0.2,
            }}
          />
        ))}
        <div
          aria-hidden
          className="absolute inset-x-0 text-center text-[21px] leading-none font-semibold text-[var(--ink)]"
          style={{ bottom: -2 }}
        >
          {reading.percent === null ? (
            <span className="text-[13px] font-semibold text-[var(--ink-2)]">No data</span>
          ) : (
            `${reading.percent}%`
          )}
        </div>
      </div>
      <table className="sr-only">
        <caption>Answered by chatbot</caption>
        <tbody>
          <tr>
            <th scope="row">Rate</th>
            <td>{reading.percent === null ? "No data" : `${reading.percent}%`}</td>
          </tr>
        </tbody>
      </table>
      <p className="mt-[3px] text-[12.5px] text-[var(--ink-2)]">Answered by chatbot</p>
    </div>
  );
}

/**
 * Reference Dashboard.dc.html:128-134 -- the big number and the arrow share
 * one `space-between` row, with the 12.5px label BELOW them (the shipped
 * version had the label above the number, which inverted the artboard's
 * hierarchy). `.stat-num` is `font-size:32px; font-weight:600;
 * letter-spacing:-0.02em; line-height:1`.
 */
function StatReadout({ label, value, href, caption }: { label: string; value: number; href: string; caption: string }) {
  return (
    <Link
      href={href}
      aria-label={`${label}: ${value.toLocaleString()}. ${caption}`}
      className="group flex flex-col justify-center gap-[7px] rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
    >
      <span className="flex items-center justify-between gap-2">
        <span className="text-[32px] leading-none font-semibold tracking-[-0.02em] tabular-nums text-[var(--ink)]">
          {value.toLocaleString()}
        </span>
        <ArrowRight
          aria-hidden
          className="size-[17px] shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5"
        />
      </span>
      <span className="text-[12.5px] text-[var(--ink-2)]">{label}</span>
      <span className="sr-only">{caption}</span>
    </Link>
  );
}

/**
 * The cream hero strip. Reference `Dashboard.dc.html:89-145`:
 * `border-radius:16px; padding:16px 26px; gap:14px`, then a single row split
 * into four zones by 1px `--line-2` rules at flex ratios 1.35 / 1 / 0.85 /
 * 0.85. D8 still applies: the row stacks on small screens (the artboard's
 * fixed 902px strip is not responsive), and the vertical rules only draw at
 * `lg:` where there is a real column boundary to draw between.
 */
export function DashboardHero({ hub, title }: { hub: ChatbotHub; title: string }) {
  return (
    <section
      aria-label="Chatbot hub overview"
      className="flex flex-col gap-3.5 rounded-2xl bg-[var(--secondary)] px-5 py-4 sm:px-[26px]"
    >
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="mb-[5px] text-[10.5px] font-semibold tracking-[0.12em] text-muted-foreground uppercase">
            Chatbot Hub
          </p>
          <h1 className="text-[29px] leading-none font-semibold tracking-[-0.01em] text-[var(--ink)]">{title}</h1>
        </div>
        {/* `.icon-btn` (40x40, radius 11) and `.pill-btn.pill-primary`
            (height 40, radius 11, 14px/600) -- Dashboard.dc.html:32-36. */}
        <div className="flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            aria-label="Filter dashboard"
            className="flex size-10 items-center justify-center rounded-[11px] border border-[var(--line-2)] bg-card text-[var(--ink-2)] transition-colors hover:bg-[#e6e6e6] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <Filter aria-hidden className="size-[17px]" />
          </button>
          <Link
            href="/conversations"
            className="inline-flex h-10 items-center justify-center gap-2 rounded-[11px] bg-primary px-[18px] text-sm font-semibold text-primary-foreground transition-colors hover:bg-[var(--ink-2)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            Review Conversations
            <ArrowRight aria-hidden className="size-[15px]" />
          </Link>
        </div>
      </div>

      <div className="flex flex-col gap-6 sm:grid sm:grid-cols-2 lg:flex lg:flex-row lg:items-center lg:gap-0">
        <div className="flex flex-col lg:flex-[1.35] lg:pr-[26px]">
          <p className="text-[13px] font-medium text-[var(--ink-2)]">New Conversations</p>
          <WeeklyConversationBars hub={hub} />
        </div>
        <div aria-hidden className="hidden self-stretch bg-[var(--line-2)] lg:block lg:my-1 lg:w-px" />
        <div className="flex flex-col items-center lg:flex-1 lg:px-6">
          <AnsweredByChatbotGauge hub={hub} />
        </div>
        <div aria-hidden className="hidden self-stretch bg-[var(--line-2)] lg:block lg:my-1 lg:w-px" />
        <div className="flex flex-col justify-center lg:flex-[0.85] lg:px-6">
          <StatReadout
            label="Active conversations"
            value={hub.activeConversations.total}
            href="/conversations?status=active"
            caption="Currently open."
          />
        </div>
        <div aria-hidden className="hidden self-stretch bg-[var(--line-2)] lg:block lg:my-1 lg:w-px" />
        <div className="flex flex-col justify-center lg:flex-[0.85] lg:pl-6">
          <StatReadout
            label="Closed conversations"
            value={hub.closedConversations.total}
            href="/conversations?status=ended"
            caption="Conversations that have ended."
          />
        </div>
      </div>
    </section>
  );
}
