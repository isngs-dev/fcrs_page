/**
 * Stat-tile row for the analytics dashboard: 4 stat cards, the last one on
 * a near-black surface with a white headline number (SR-22: migrated off
 * the superseded legacy palette onto the SR-15 design tokens).
 *
 * Every number here is a REAL metric already computed by
 * `services/api/src/api/analytics/repository.py` (S13.5 decision 4/6) --
 * no invented figures. Where the mock's text names a metric this backend
 * does not compute ("Leads captured"), that card is intentionally NOT
 * built here (see the honest-gap accounting in the restyle report) rather
 * than faked with borrowed data from another module.
 *
 * Card mapping (5b mock -> real field):
 *   1. CONVERSATIONS      -> totals.conversations
 *   2. ANSWERED W/O ESCALATION (deflection) -> deflectionRate, now a RING GAUGE
 *   3. GROUNDED RATE       -> groundedRate, now a RING GAUGE
 *   4. CALLS BOOKED (ink/citron) -> schedule.conversions
 *
 * SR-27 slice 9: the two rate-based cards (deflection/grounded) are swapped
 * for `RingGauge`s (r=52, stroke-width 13, round cap, per SR-23's spec) --
 * both keep an explicit "No data" state for a `null` rate, never a
 * fabricated 0%-filled ring (CLAUDE.md §3, Decision 6a).
 */
import { formatRate, type AnalyticsOverview } from "@/lib/analytics";
import { RingGauge } from "@/app/(protected)/analytics/ring-gauge";

function StatCard({
  label,
  value,
  caption,
  captionTone = "muted",
  ink = false,
}: {
  label: string;
  value: string;
  caption: string;
  captionTone?: "muted" | "positive" | "negative";
  ink?: boolean;
}) {
  const captionColor = ink
    ? "text-white/60"
    : captionTone === "positive"
      ? "text-[var(--success-fg)]"
      : captionTone === "negative"
        ? "text-[var(--danger-fg)]"
        : "text-muted-foreground";

  return (
    <div
      className={
        ink
          ? "flex flex-col gap-1.5 rounded-[14px] bg-foreground p-4"
          : "flex flex-col gap-1.5 rounded-[14px] border border-border bg-card p-4 shadow-[0_1px_2px_rgba(28,27,25,.03)]"
      }
    >
      <span
        className={
          ink
            ? "text-[11.5px] font-semibold text-white/60"
            : "text-[11.5px] font-semibold text-muted-foreground"
        }
      >
        {label.toUpperCase()}
      </span>
      <span
        className={
          // SR-15 D1: the ink-background variant's citron value text is
          // deleted and re-decided to white -- it already reads as the
          // emphasized number on a dark card fill.
          ink
            ? "text-[30px] font-bold tabular-nums text-white"
            : "text-[30px] font-bold tabular-nums text-foreground"
        }
      >
        {value}
      </span>
      <span className={`text-[11.5px] font-semibold ${captionColor}`}>{caption}</span>
    </div>
  );
}

/**
 * Raw supporting totals (user/bot/decided turns, schedule CTA count) that
 * back the headline cards -- kept as a compact secondary strip rather than
 * dropped, since they're real and useful for debugging a rate that looks
 * off, but they are not part of 5b's 4-card hero row.
 */
function MiniTotal({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-[10px] border border-border bg-secondary px-3 py-2">
      <span className="text-[10.5px] font-semibold tracking-wide text-muted-foreground uppercase">
        {label}
      </span>
      <span className="text-[15px] font-bold tabular-nums text-foreground">{value}</span>
    </div>
  );
}

export function AnalyticsCards({ data }: { data: AnalyticsOverview }) {
  const deflectedCount =
    data.deflectionRate === null
      ? null
      : Math.round(data.deflectionRate * data.totals.conversations);

  return (
    <div className="flex flex-col gap-3">
      <div
        className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4"
        role="group"
        aria-label="Analytics summary stats"
      >
        <StatCard
          label="Conversations"
          value={data.totals.conversations.toLocaleString()}
          caption={`${data.totals.userTurns.toLocaleString()} visitor turns in this window`}
        />
        {/* SR-27 slice 9: the two rate cards become ring gauges
            (SR-23 spec, r=52/stroke-13/round-cap). Both real fields
            (`deflection_rate`/`grounded_rate`, `analytics/routes.py:88-89`);
            a `null` rate renders the gauge's own "No data" state, never a
            0%-filled ring. */}
        <RingGauge
          label="Answered without escalation"
          rate={data.deflectionRate}
          caption={
            data.deflectionRate === null
              ? "No conversations in this window."
              : `${deflectedCount?.toLocaleString()} of ${data.totals.conversations.toLocaleString()} deflected`
          }
        />
        <RingGauge
          label="Grounded rate"
          rate={data.groundedRate}
          caption={
            data.groundedRate === null
              ? "No answered turns in this window."
              : "Share of answers grounded in retrieved knowledge"
          }
        />
        <StatCard
          label="Calls booked"
          value={data.schedule.conversions.toLocaleString()}
          caption={
            data.schedule.ctaConversations === 0
              ? "No scheduling CTA shown in this window."
              : `${formatRate(data.schedule.conversionRate)} of ${data.schedule.ctaConversations.toLocaleString()} CTA conversations (approximate)`
          }
          ink
        />
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        <MiniTotal label="User turns" value={data.totals.userTurns} />
        <MiniTotal label="Bot turns" value={data.totals.botTurns} />
        <MiniTotal label="Decided bot turns" value={data.totals.decidedBotTurns} />
        <MiniTotal label="Schedule CTAs shown" value={data.schedule.ctaConversations} />
      </div>
    </div>
  );
}
