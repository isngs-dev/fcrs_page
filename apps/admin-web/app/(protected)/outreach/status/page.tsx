/**
 * Lead status (A13 R1/R5, CLIENT_ADMIN): where every person Rebecca works on stands --
 * the scope's nine states, derived from what actually happened.
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { getStatuses } from "@/lib/outreach";
import { OUTREACH_STATUSES, camelKey } from "@/lib/outreach-format";
import { TableCard, TableCell, TableHeadCell, TableRow } from "@/components/admin/table-card";
import { EmptyCard, ErrorCard, FilterPills, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";

type SearchParams = Promise<{ [key: string]: string | string[] | undefined }>;
const LABEL = Object.fromEntries(OUTREACH_STATUSES.map((s) => [s.value, s.label]));

export default async function LeadStatusPage({ searchParams }: { searchParams: SearchParams }) {
  await requireRole("CLIENT_ADMIN");
  const raw = (await searchParams).status;
  const picked = Array.isArray(raw) ? raw[0] : raw;
  const status = picked && LABEL[picked] ? picked : undefined;
  const result = await getStatuses(status);

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Lead status" subtitle="Where every person Rebecca is following up stands right now." />
      {result.status === "error" ? (
        <ErrorCard message={result.message} correlationId={result.correlationId} />
      ) : (
        <>
          <FilterPills
            active={status}
            pills={[
              { label: `All (${Object.values(result.data.counts).reduce((a, b) => a + b, 0)})`, href: "/outreach/status" },
              ...OUTREACH_STATUSES.map((s) => ({
                label: `${s.label} (${result.data.counts[camelKey(s.value)] ?? 0})`,
                value: s.value,
                href: `/outreach/status?status=${s.value}`,
              })),
            ]}
          />
          {result.data.contacts.length === 0 ? (
            <EmptyCard title="Nobody here" body="People appear once Rebecca starts following them up." />
          ) : (
            <TableCard className="overflow-x-auto">
              <thead>
                <tr>
                  <TableHeadCell>Name</TableHeadCell>
                  <TableHeadCell>Email</TableHeadCell>
                  <TableHeadCell>Status</TableHeadCell>
                </tr>
              </thead>
              <tbody>
                {result.data.contacts.map((c) => (
                  <TableRow key={c.contactId}>
                    <TableCell>
                      {c.leadId ? (
                        <Link href={`/leads?lead=${encodeURIComponent(c.leadId)}&tab=agents`} className="font-semibold">
                          {c.name ?? "—"}
                        </Link>
                      ) : (
                        <Link href={`/outreach/contacts/${encodeURIComponent(c.contactId)}`} className="font-semibold">
                          {c.name ?? "—"}
                        </Link>
                      )}
                    </TableCell>
                    <TableCell>{c.email ?? "—"}</TableCell>
                    <TableCell>{LABEL[c.status] ?? c.status}</TableCell>
                  </TableRow>
                ))}
              </tbody>
            </TableCard>
          )}
        </>
      )}
    </div>
  );
}
