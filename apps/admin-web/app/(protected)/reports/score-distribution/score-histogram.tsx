/**
 * Lead-score-distribution histogram (SR-19 D8/D5, orientation flipped
 * SR-27 slice 6 per `Console.dc.html`'s vertical-columns recipe). Five
 * proportional COLUMNS (fixed 20-point bands, comparable across windows,
 * count printed above each bar) + an explicit "unscored" readout --
 * `qualification_score IS NULL` is counted separately, NEVER folded into
 * the 0-19 band, because a lead nobody scored is not a lead that scored
 * badly. Data logic is unchanged from the prior horizontal-bar version --
 * only the axis orientation moved. `role="img"` + a full `aria-label`, a
 * visually-hidden `<table>` of exact values.
 */
import type { ScoreDistributionReport } from "@/lib/reports";

const BAND_ORDER = ["0-19", "20-39", "40-59", "60-79", "80-100"] as const;

function Column({ label, count, max, tone }: { label: string; count: number; max: number; tone: "default" | "unscored" }) {
  const pct = Math.max((count / max) * 100, count > 0 ? 4 : 1);
  return (
    <div className="flex flex-1 flex-col items-center gap-1.5">
      <span className="text-sm font-bold tabular-nums text-[var(--foreground)]">{count}</span>
      <div className="flex h-32 w-full items-end overflow-hidden rounded-[6px] bg-[var(--secondary)]">
        <div
          title={`${label}: ${count}`}
          className={tone === "unscored" ? "w-full rounded-[6px] bg-[#a9aa9f]" : "w-full rounded-[6px] bg-[var(--foreground)]"}
          style={{ height: `${pct}%` }}
        />
      </div>
      <span className="text-xs font-semibold text-[var(--muted-foreground)]">{label}</span>
    </div>
  );
}

export function ScoreHistogram({ data }: { data: ScoreDistributionReport }) {
  if (data.total === 0) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        No leads in this window.
      </p>
    );
  }

  const max = Math.max(...BAND_ORDER.map((b) => data.bands[b] ?? 0), data.unscored, 1);

  const ariaLabel = `Lead score distribution: ${BAND_ORDER.map(
    (b) => `${b} -- ${data.bands[b] ?? 0}`
  ).join("; ")}; Unscored -- ${data.unscored}. Total ${data.total}.`;

  return (
    <div className="flex flex-col gap-3">
      <div role="img" aria-label={ariaLabel} className="flex items-end gap-3">
        {BAND_ORDER.map((band) => (
          <Column key={band} label={band} count={data.bands[band] ?? 0} max={max} tone="default" />
        ))}

        {/* Unscored is deliberately visually separated from the bands --
            it is NOT a sixth band, it is a different question (D8). */}
        <div className="ml-2 flex flex-1 flex-col items-center gap-1.5 border-l border-dashed border-[var(--border)] pl-3">
          <span className="text-sm font-bold tabular-nums text-[var(--foreground)]">{data.unscored}</span>
          <div className="flex h-32 w-full items-end overflow-hidden rounded-[6px] bg-[var(--secondary)]">
            <div
              title={`Unscored: ${data.unscored}`}
              className="w-full rounded-[6px] bg-[#a9aa9f]"
              style={{ height: `${Math.max((data.unscored / max) * 100, data.unscored > 0 ? 4 : 1)}%` }}
            />
          </div>
          <span className="text-xs font-semibold text-[var(--muted-foreground)]">Unscored</span>
        </div>
      </div>

      <table className="sr-only">
        <caption>Lead score distribution -- exact counts</caption>
        <thead>
          <tr>
            <th scope="col">Band</th>
            <th scope="col">Count</th>
          </tr>
        </thead>
        <tbody>
          {BAND_ORDER.map((band) => (
            <tr key={band}>
              <td>{band}</td>
              <td>{data.bands[band] ?? 0}</td>
            </tr>
          ))}
          <tr>
            <td>Unscored</td>
            <td>{data.unscored}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
