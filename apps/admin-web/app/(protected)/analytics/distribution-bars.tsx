/**
 * Renders a `Record<string, number>` distribution (currently: intent only,
 * SR-27 slice 9 -- decision distribution moved to `DecisionMixDonut`) as a
 * descending-count progress list, styled to the current reference's
 * (`Console.dc.html`) "top intents" thin-bar-list visual language -- track +
 * fill bar, count at right, label at left, top item's fill in solid black.
 *
 * Honesty note: the reference literally shows question text ("Pricing &
 * plans", "Integrations", ...). This backend does not track per-question
 * text/frequency -- `intent_distribution` is a closed-set of *intent
 * categories* (repository.py `_fetch_message_facts`, e.g. "pricing",
 * "unclassified"), not verbatim visitor questions. Labeling this list
 * "Top questions" would misrepresent what's real, so the caller titles it
 * "Top intents" instead -- see the separate `UnavailableCard` for the
 * honest gap on literal top questions (G14, re-verified SR-27).
 */
export function DistributionBars({
  title,
  data,
}: {
  title: string;
  data: Record<string, number>;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = entries.length > 0 ? Math.max(...entries.map(([, count]) => count)) : 0;

  return (
    <div className="flex flex-col gap-3 rounded-[14px] border border-[var(--border)] p-[18px]">
      <span className="text-sm font-bold text-[var(--foreground)]">{title}</span>
      {entries.length === 0 ? (
        <p role="status" className="text-sm text-[var(--muted-foreground)]">
          No data for this window.
        </p>
      ) : (
        <ul className="flex flex-col gap-2.5 text-[12.5px] text-[var(--ink-2)]">
          {entries.map(([label, count], i) => {
            const widthPct = max > 0 ? (count / max) * 100 : 0;
            const isTop = i === 0;
            return (
              <li key={label} className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate">{label}</span>
                  <span className="font-bold text-[var(--foreground)] tabular-nums">{count}</span>
                </div>
                <div className="h-[7px] overflow-hidden rounded-full bg-[var(--secondary)]">
                  <div
                    role="img"
                    aria-label={`${label}: ${count} of ${max} (top value)`}
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max(widthPct, count > 0 ? 4 : 0)}%`,
                      // SR-15 D1: the top-value citron fill is deleted and
                      // re-decided to full black vs. the others' -- the top
                      // bar is already distinguished by being the longest.
                      backgroundColor: isTop ? "#000000" : "var(--foreground)",
                    }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
