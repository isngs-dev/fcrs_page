/**
 * Lead-sources donut (SR-19 D4/D5). SR-27 slice 9: now a thin wrapper over
 * the shared `components/admin/donut.tsx` `Donut` primitive (genericized
 * from this component's own arc math, preferred over a second Analytics-only
 * copy per the handoff) -- this file keeps only the lead-sources-specific
 * concerns: the `singleSource` honest note and mapping `LeadSourcesReport`
 * into the primitive's generic `{label, count}` slice shape.
 *
 * D4 -- IMPORTANT: `leads.source` defaults to `'widget'` and the widget is
 * currently the platform's only lead-creation channel, so a real tenant's
 * donut is expected to show ONE slice for a long time. This is NOT a bug.
 * Do not "fix" a single-slice result by padding fake categories or
 * hardcoding a taxonomy the data does not have (see the repository
 * function's docstring and the sprint spec for the same statement, made on
 * purpose in three places). When `singleSource` is true, this component
 * shows an honest explanatory note instead of a donut that could be
 * mistaken for a rendering failure.
 */
import type { LeadSourcesReport } from "@/lib/reports";
import { Donut } from "@/components/admin/donut";

export function LeadSourcesDonut({ data }: { data: LeadSourcesReport }) {
  return (
    <div className="flex flex-col gap-4">
      {data.singleSource ? (
        <p role="status" className="text-[13px] text-[var(--muted-foreground)]">
          All leads in this window came from one source. This is expected -- the widget is
          currently the only lead-creation channel.
        </p>
      ) : null}
      <Donut
        slices={data.sources.map((s) => ({ label: s.source, count: s.count }))}
        total={data.total}
        centerCaption="leads"
        ariaLabelPrefix="Lead sources"
        emptyMessage="No leads in this window."
      />
    </div>
  );
}
