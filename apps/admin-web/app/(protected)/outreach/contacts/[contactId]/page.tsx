/**
 * A contact's Agents view (plan, Take over, emails, calls, what Rebecca knows) for people
 * with no lead record -- added by hand or by CSV -- who therefore have no lead drawer.
 * The backend applies the visibility rules (tenant; agents see only their contacts).
 * Admins can also erase what Rebecca holds on the person (A9 data rights, audited).
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { eraseAgentData, getContactActivity } from "@/lib/outreach";
import { AgentsTab } from "@/app/(protected)/leads/lead-agents-tab";
import { ActionButton } from "@/app/(protected)/outreach/action-button";
import { ErrorCard, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";

export default async function ContactAgentsPage({ params }: { params: Promise<{ contactId: string }> }) {
  const claims = await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");
  const { contactId } = await params;
  const result = await getContactActivity(contactId);

  return (
    <div className="flex flex-col gap-5">
      <Link href="/outreach/status" className="text-[12.5px] text-muted-foreground">
        ← Lead status
      </Link>
      {result.status === "error" ? (
        <ErrorCard message={result.message} correlationId={result.correlationId} />
      ) : (
        <>
          <OutreachHeader
            title={result.activity.name ?? "Contact"}
            subtitle={result.activity.email ?? "No email on file"}
          />
          <AgentsTab result={result} platformView={false} />
          {claims.role === "CLIENT_ADMIN" ? (
            <div className="flex flex-col gap-1">
              <ActionButton
                action={eraseAgentData.bind(null, contactId)}
                label="Erase Rebecca's data"
                variant="destructive"
                confirmText="Permanently delete Rebecca's plans, emails, calls, recordings and memory for this person? Their contact details and consent records stay."
              />
              <p className="text-[12px] text-muted-foreground">
                Their contact details and consent records stay, so adding them again matches this contact.
              </p>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
