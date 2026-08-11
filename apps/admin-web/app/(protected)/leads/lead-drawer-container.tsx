/**
 * Server-side data fetch for the 4b drawer. `page.tsx`/`clients/[tenantId]/
 * leads/page.tsx` read `?lead=<id>&tab=<tab>` from `searchParams` and, when
 * present, render this async server component -- it fetches the lead detail
 * (+ activities, needed by both the Activity and Notes tabs) and hands them
 * to the client `LeadDrawer` for interactivity (tabs, Esc/focus).
 *
 * SR-17 D3/scope item 9, SR-24: the Timeline tab renders the SAME shared
 * `RecordDrawerTimelinePanel` the Contact record drawer uses, fed by
 * `GET /admin/leads/{lead_id}/timeline` -- the only difference between the
 * Contact and Lead timeline views is which endpoint supplied the items,
 * exactly mirroring the backend's shared `_impl`. `?before=` (cursor
 * pagination, scope item 10) is read here too. SR-24 made Timeline the
 * DEFAULT tab (was Transcript, now deleted) and dropped the Activity tab
 * (a strict subset of Timeline's `lead_activity` items) -- `activitiesResult`
 * is now fetched only for Notes, which still needs it to list/count notes.
 */
import { getLeadActivities, getLeadDetail } from "@/lib/leads";
import { getLeadTimeline } from "@/lib/timeline";
import { LeadDrawer, TABS, type Tab } from "@/app/(protected)/leads/lead-drawer";

function isTab(value: string | undefined): value is Tab {
  return !!value && (TABS as readonly string[]).includes(value);
}

export async function LeadDrawerContainer({
  leadId,
  rawTab,
  basePath,
  tenantId,
  before,
}: {
  leadId: string;
  rawTab: string | undefined;
  basePath: string;
  tenantId?: string;
  before?: string;
}) {
  const tab: Tab = isTab(rawTab) ? rawTab : "timeline";

  const detailResult = await getLeadDetail(leadId, tenantId);
  // Notes needs the activities list to render/count notes; Details/Timeline
  // don't, so skip the extra round trip when Notes isn't the active tab.
  const activitiesResult = tab === "notes" ? await getLeadActivities(leadId, tenantId) : null;
  const timelineResult = tab === "timeline" ? await getLeadTimeline(leadId, { before }) : null;

  return (
    <LeadDrawer
      leadId={leadId}
      tab={tab}
      detailResult={detailResult}
      activitiesResult={activitiesResult}
      timelineResult={timelineResult}
      basePath={basePath}
      tenantId={tenantId}
    />
  );
}
