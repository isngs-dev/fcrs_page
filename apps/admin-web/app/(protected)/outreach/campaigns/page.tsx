/**
 * Campaigns (A8 U8/U9): start outreach three ways (admins + agents), and --
 * admins only -- the campaign list with a Stop per campaign. There is no
 * "pause": nothing reads campaign status; the kill switch pauses everything.
 */
import { requireAnyRole } from "@/lib/auth";
import { listCampaigns, stopCampaign } from "@/lib/outreach";
import { formatWhen, humanize } from "@/lib/outreach-format";
import { TableCard, TableCell, TableHeadCell, TableRow } from "@/components/admin/table-card";
import { EmptyCard, ErrorCard, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";
import { ActionButton } from "@/app/(protected)/outreach/action-button";
import { AddContactCard, ImportCard, RebeccaCard } from "@/app/(protected)/outreach/campaigns/start-outreach";
import { CampaignEndDate } from "@/app/(protected)/outreach/campaigns/campaign-end-date";

export default async function OutreachCampaignsPage() {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");
  const campaigns = claims.role === "CLIENT_ADMIN" ? await listCampaigns() : null;

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Campaigns" subtitle="Start follow-up for new contacts, and stop a campaign at any time." />
      <div className="grid gap-4 xl:grid-cols-2">
        <ImportCard />
        <div className="flex flex-col gap-4">
          <AddContactCard />
          <RebeccaCard />
        </div>
      </div>

      {campaigns === null ? null : campaigns.status === "error" ? (
        <ErrorCard message={campaigns.message} correlationId={campaigns.correlationId} />
      ) : campaigns.campaigns.length === 0 ? (
        <EmptyCard title="No campaigns yet" body="Each import, added contact or Rebecca request starts a campaign." />
      ) : (
        <TableCard className="overflow-x-auto">
          <thead>
            <tr>
              <TableHeadCell>Campaign</TableHeadCell>
              <TableHeadCell>Source</TableHeadCell>
              <TableHeadCell>Active</TableHeadCell>
              <TableHeadCell>Stopped</TableHeadCell>
              <TableHeadCell>Completed</TableHeadCell>
              <TableHeadCell>Started</TableHeadCell>
              <TableHeadCell>Ends</TableHeadCell>
              <TableHeadCell>
                <span className="sr-only">Actions</span>
              </TableHeadCell>
            </tr>
          </thead>
          <tbody>
            {campaigns.campaigns.map((c) => (
              <TableRow key={c.campaignId}>
                <TableCell>
                  <span className="font-semibold">{c.name}</span>
                  {c.status !== "active" ? <span className="ml-2 text-muted-foreground">({humanize(c.status)})</span> : null}
                  {c.goal ? <span className="block text-[12px] text-muted-foreground">{c.goal}</span> : null}
                </TableCell>
                <TableCell>{humanize(c.kind)}</TableCell>
                <TableCell>{c.plansActive}</TableCell>
                <TableCell>{c.plansStopped}</TableCell>
                <TableCell>{c.plansCompleted}</TableCell>
                <TableCell>{formatWhen(c.createdAt)}</TableCell>
                <TableCell>
                  {c.status === "active" ? (
                    <CampaignEndDate campaignId={c.campaignId} endsAt={c.endsAt} />
                  ) : (
                    formatWhen(c.endsAt)
                  )}
                </TableCell>
                <TableCell>
                  {c.plansActive > 0 ? (
                    <ActionButton
                      action={stopCampaign.bind(null, c.campaignId)}
                      label="Stop"
                      variant="destructive"
                      confirmText={`Stop all ${c.plansActive} active follow-ups in "${c.name}"?`}
                    />
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </tbody>
        </TableCard>
      )}
    </div>
  );
}
