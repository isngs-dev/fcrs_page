/**
 * Server-side data fetch for the deal record drawer (SR-18 scope item 7).
 * Mirrors `contacts/contact-drawer-container.tsx`'s shape: `page.tsx` reads
 * `?deal=` from `searchParams` and, when present, renders this async server
 * component, which fetches the deal detail and hands it to the client
 * `DealDrawer`. No timeline fetch here -- see `deal-drawer.tsx`'s header
 * comment for why (no opportunity timeline item kind exists yet).
 */
import { getDealDetail } from "@/lib/deals";
import { DealDrawer } from "@/app/(protected)/deals/deal-drawer";

export async function DealDrawerContainer({
  opportunityId,
  basePath,
}: {
  opportunityId: string;
  basePath: string;
}) {
  const detailResult = await getDealDetail(opportunityId);

  return <DealDrawer opportunityId={opportunityId} detailResult={detailResult} basePath={basePath} />;
}
