/**
 * Recent-conversions table (SR-19 D6). SR-15's `TableCard` primitive:
 * Lead / Source / Stage / When -- deliberately NO Value column. A lead has
 * no monetary value; money lives on `opportunities.amount`, a different
 * entity reached only by a cross-entity JOIN this report's backend query is
 * forbidden from making (SR-9.5 D11). Omitting the column is the only
 * option CLAUDE.md §3 permits -- inventing a number, or rendering an
 * em-dash column, are both ruled out.
 */
import type { RecentConversionsReport } from "@/lib/reports";
import { TableCard, TableCell, TableHeadCell, TableRow } from "@/components/admin/table-card";
import { Chip } from "@/components/admin/chip";

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function RecentConversionsTable({ data }: { data: RecentConversionsReport }) {
  if (data.conversions.length === 0) {
    return (
      <p role="status" className="text-sm text-[var(--muted-foreground)]">
        No conversions in this window.
      </p>
    );
  }

  return (
    <TableCard>
      <thead>
        <tr>
          <TableHeadCell>Lead</TableHeadCell>
          <TableHeadCell>Source</TableHeadCell>
          <TableHeadCell>Stage</TableHeadCell>
          <TableHeadCell>When</TableHeadCell>
        </tr>
      </thead>
      <tbody>
        {data.conversions.map((c) => (
          <TableRow key={c.leadId}>
            <TableCell className="font-semibold text-[var(--foreground)]">{c.name}</TableCell>
            <TableCell>{c.source}</TableCell>
            <TableCell>
              <Chip tone="success" dot>
                {c.stage}
              </Chip>
            </TableCell>
            <TableCell>{formatDate(c.convertedAt)}</TableCell>
          </TableRow>
        ))}
      </tbody>
    </TableCard>
  );
}
