/**
 * Deals table view (SR-18 scope items 5-6). Columns per D1: name, contact,
 * account, amount + its row currency (D6), stage chip, expected close date,
 * owner. `contact`/`account` render as links to the Contacts/Accounts list
 * pages (mirroring `contacts-table.tsx`'s D5 posture: `Deal` only carries
 * `contactId`/`accountId`, not resolved names, and resolving them here would
 * be the N+1 fan-out that pattern deliberately avoids).
 *
 * `win_probability` is rendered as a plain derived badge -- no input, no
 * slider, nothing editable (D6, carrying SR-9.4 D2 forward). NO column
 * totals anywhere in this component (D6 -- no client-side money
 * arithmetic).
 */
import Link from "next/link";
import type { Deal } from "@/lib/deals";
import { dealStageBadgeStyle, formatDealAmount, formatDealDate } from "@/lib/deals-presentation";
import { TableCard, TableHeadCell, TableCell, TableRow } from "@/components/admin/table-card";

const MUTED = <span className="text-muted-foreground">—</span>;

function dealHref(basePath: string, currentParams: URLSearchParams, opportunityId: string): string {
  const params = new URLSearchParams(currentParams);
  params.set("deal", opportunityId);
  return `${basePath}?${params.toString()}`;
}

const HEADERS = ["Name", "Contact", "Account", "Amount", "Stage", "Expected close", "Owner"];

export function DealsTable({
  items,
  basePath,
  currentParams,
  selectedDealId,
}: {
  items: Deal[];
  basePath?: string;
  currentParams?: URLSearchParams;
  selectedDealId?: string;
}) {
  const resolvedBasePath = basePath ?? "/deals";
  const resolvedParams = currentParams ?? new URLSearchParams();

  return (
    <TableCard>
      <thead>
        <tr>
          {HEADERS.map((header) => (
            <TableHeadCell key={header}>{header}</TableHeadCell>
          ))}
        </tr>
      </thead>
      <tbody>
        {items.map((deal) => {
          const badge = dealStageBadgeStyle(deal.stage);
          const highlighted = deal.opportunityId === selectedDealId;
          return (
            <TableRow key={deal.opportunityId} style={highlighted ? { background: "#fdfdec" } : undefined}>
              <TableCell className="px-0 py-0">
                <Link
                  href={dealHref(resolvedBasePath, resolvedParams, deal.opportunityId)}
                  scroll={false}
                  className="block min-h-11 px-3.5 py-3 font-bold text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                >
                  {deal.name}
                </Link>
              </TableCell>
              <TableCell>
                <Link
                  href="/contacts"
                  className="text-[var(--ink-2)] underline underline-offset-2"
                  title={`Contact ${deal.contactId} — there is no per-contact filtered deal view yet; this links to the Contacts list.`}
                >
                  View contact
                </Link>
              </TableCell>
              <TableCell>
                {deal.accountId ? (
                  <Link
                    href="/accounts"
                    className="text-[var(--ink-2)] underline underline-offset-2"
                    title={`Account ${deal.accountId} — there is no per-account detail page yet; this links to the Accounts list.`}
                  >
                    View account
                  </Link>
                ) : (
                  MUTED
                )}
              </TableCell>
              <TableCell className="font-semibold text-[var(--ink-2)]">
                {formatDealAmount(deal.amount, deal.currency)}
              </TableCell>
              <TableCell>
                <span
                  className="rounded-full px-2.5 py-[3px] text-[10.5px] font-bold"
                  style={{ background: badge.bg, color: badge.fg }}
                >
                  {badge.label}
                </span>
              </TableCell>
              <TableCell className="text-muted-foreground">{formatDealDate(deal.expectedCloseDate)}</TableCell>
              <TableCell className="text-[var(--ink-2)]">{deal.ownerAgentId || MUTED}</TableCell>
            </TableRow>
          );
        })}
      </tbody>
    </TableCard>
  );
}
