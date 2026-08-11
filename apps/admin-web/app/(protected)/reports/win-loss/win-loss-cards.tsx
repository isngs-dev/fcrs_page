/**
 * Win/loss stat cards + plain (never grouped) loss-reason list (SR-9.5 D13).
 * `win_probability` deliberately does not exist anywhere in this component
 * or the data it renders.
 */
import { formatRate } from "@/lib/analytics";
import type { WinLossReport } from "@/lib/reports";

function formatMoney(amount: string, currency: string): string {
  const value = Number(amount);
  if (Number.isNaN(value)) return `${amount} ${currency}`;
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(value);
}

function StatCard({ label, value, caption }: { label: string; value: string; caption?: string }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-[14px] border border-[var(--border)] p-4">
      <span className="text-[11.5px] font-semibold text-[var(--muted-foreground)] uppercase">{label}</span>
      <span className="text-[26px] font-bold tabular-nums text-[var(--foreground)]">{value}</span>
      {caption ? <span className="text-[11.5px] text-[var(--muted-foreground)]">{caption}</span> : null}
    </div>
  );
}

function OutcomeSection({
  title,
  outcome,
  currency,
}: {
  title: string;
  outcome: WinLossReport["won"];
  currency: string;
}) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatCard label={`${title} deals`} value={outcome.count.toLocaleString()} />
      <StatCard
        label={`${title} total`}
        value={formatMoney(outcome.amountTotal, currency)}
        caption={outcome.amountNullCount > 0 ? `${outcome.amountNullCount} unquoted (excluded)` : undefined}
      />
      <StatCard
        label="Avg deal size"
        value={outcome.avgDealSize === null ? "No data" : formatMoney(outcome.avgDealSize, currency)}
      />
      <StatCard
        label="Avg days to close"
        value={outcome.avgDaysToClose === null ? "No data" : outcome.avgDaysToClose.toFixed(1)}
      />
    </div>
  );
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function WinLossCards({ data }: { data: WinLossReport }) {
  const noData = data.won.count === 0 && data.lost.count === 0;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="rounded-[8px] bg-[var(--secondary)] px-2.5 py-1 font-semibold text-[var(--ink-2)]">
          Currency: {data.currency}
        </span>
        {!data.currencyConfigured ? (
          <span
            role="status"
            className="rounded-[8px] border border-dashed border-[#d5d5cb] px-2.5 py-1 text-[var(--muted-foreground)]"
          >
            Currency not configured for this tenant -- defaulted to {data.currency}.
          </span>
        ) : null}
        <span className="ml-auto font-semibold text-[var(--foreground)]">
          Win rate: {formatRate(data.winRate)}
        </span>
      </div>

      {noData ? (
        <p role="status" className="text-sm text-[var(--muted-foreground)]">
          No data in this window.
        </p>
      ) : (
        <>
          <div className="flex flex-col gap-2">
            <span className="text-sm font-bold text-[var(--foreground)]">Won</span>
            <OutcomeSection title="Won" outcome={data.won} currency={data.currency} />
          </div>
          <div className="flex flex-col gap-2">
            <span className="text-sm font-bold text-[var(--foreground)]">Lost</span>
            <OutcomeSection title="Lost" outcome={data.lost} currency={data.currency} />
          </div>
        </>
      )}

      <div className="flex flex-col gap-2 border-t border-[var(--secondary)] pt-4">
        <span className="text-sm font-bold text-[var(--foreground)]">Loss reasons</span>
        <p className="text-[11.5px] text-[var(--muted-foreground)]">
          Raw, ungrouped, reverse-chronological -- not a taxonomy.
        </p>
        {data.lossReasons.length === 0 ? (
          <p role="status" className="text-sm text-[var(--muted-foreground)]">
            No losses in this window.
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {data.lossReasons.map((reason, i) => (
              <li
                key={`${reason.closedAt}-${i}`}
                className="flex flex-col gap-0.5 rounded-[10px] border border-[var(--secondary)] px-3.5 py-2.5 text-sm"
              >
                <span className="text-[11px] text-[var(--muted-foreground)]">{formatDate(reason.closedAt)}</span>
                <span className="text-[var(--ink-2)]">{reason.closeReason ?? "(no reason given)"}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
