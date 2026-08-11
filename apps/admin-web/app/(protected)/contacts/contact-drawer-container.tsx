/**
 * Server-side data fetch for the contact record drawer (SR-17 D3). Mirrors
 * `leads/lead-drawer-container.tsx`'s shape: `page.tsx` reads `?contact=`
 * from `searchParams` and, when present, renders this async server
 * component, which fetches the contact detail + timeline and hands them to
 * the shared client `RecordDrawer`.
 *
 * `?before=` (scope item 10, cursor pagination) is also read here and
 * forwarded to `getContactTimeline` -- an older page is a fresh server
 * fetch via the URL, never client-accumulated state.
 */
import { getContactDetail } from "@/lib/contacts";
import { getContactTimeline } from "@/lib/timeline";
import { ContactDrawer } from "@/app/(protected)/contacts/contact-drawer";

export async function ContactDrawerContainer({
  contactId,
  before,
  basePath,
}: {
  contactId: string;
  before: string | undefined;
  basePath: string;
}) {
  const detailResult = await getContactDetail(contactId);
  const timelineResult = await getContactTimeline(contactId, { before });

  return (
    <ContactDrawer
      contactId={contactId}
      detailResult={detailResult}
      timelineResult={timelineResult}
      basePath={basePath}
    />
  );
}
