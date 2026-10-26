/**
 * Lead drawer "Agents" tab (A8 U11): the lead's follow-up plan, consents per
 * channel, Rebecca's emails and calls, and "Take over" (stops the plan via a
 * `human_takeover` event). Rendered inside the client `LeadDrawer`.
 */
import Link from "next/link";
import { takeOver, type LeadActivity, type Result } from "@/lib/outreach";
import { formatDuration, formatWhen, humanize } from "@/lib/outreach-format";
import { ActionButton } from "@/app/(protected)/outreach/action-button";

const MEMORY_LABELS: Record<string, string> = {
  asked: "Asked",
  need: "Told us",
  objection: "Concern",
  we_said: "We said",
  booked: "Booking",
  summary: "Call summary",
};

export type LeadActivityResult = Result<{ activity: LeadActivity }>;

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-[12px] font-semibold tracking-[.04em] text-muted-foreground uppercase">{title}</h3>
      {children}
    </section>
  );
}

export function AgentsTab({ result, platformView }: { result: LeadActivityResult | null; platformView: boolean }) {
  const panel = (children: React.ReactNode) => (
    <div id="lead-tabpanel-agents" role="tabpanel" aria-labelledby="lead-tab-agents" className="flex flex-col gap-5 px-[22px] py-[18px]">
      {children}
    </div>
  );
  if (platformView) {
    return panel(<p className="text-sm text-muted-foreground">Switch to this chatbot to see Rebecca&apos;s activity for this lead.</p>);
  }
  if (result === null) return panel(<p className="text-sm text-muted-foreground">Loading…</p>);
  if (result.status === "error") {
    return panel(
      <p role="alert" className="text-sm text-destructive">
        {result.message}
      </p>
    );
  }
  const { activity } = result;
  if (!activity.contactId) {
    return panel(<p className="text-sm text-muted-foreground">No agent activity for this lead yet.</p>);
  }
  const contactId = activity.contactId;
  const plan = activity.plan;

  return panel(
    <>
      <Section title="Follow-up plan">
        {plan ? (
          <div className="flex flex-col gap-2 text-[13px]">
            <p>
              <span className="font-semibold">{humanize(plan.status)}</span>
              {plan.stopReason ? <span className="text-muted-foreground"> — {humanize(plan.stopReason)}</span> : null}
            </p>
            <ol className="flex flex-col gap-1">
              {plan.tasks.map((t) => (
                <li key={t.taskId} className="flex justify-between gap-2 text-[12.5px]">
                  <span>
                    {t.stepIndex + 1}. {humanize(t.channel)}
                  </span>
                  <span className="text-muted-foreground">
                    {humanize(t.status)} · {formatWhen(t.scheduledAt)}
                  </span>
                </li>
              ))}
            </ol>
            {plan.status === "active" ? (
              <ActionButton
                action={takeOver.bind(null, contactId)}
                label="Take over (stop Rebecca)"
                variant="outline"
                confirmText="Stop Rebecca's follow-up for this lead? You'll handle them yourself."
              />
            ) : null}
          </div>
        ) : (
          <p className="text-[12.5px] text-muted-foreground">No follow-up plan.</p>
        )}
      </Section>

      {activity.memory && activity.memory.length ? (
        <Section title="What Rebecca knows">
          <ul className="flex flex-col gap-1 text-[12.5px]">
            {activity.memory.slice(-8).reverse().map((item, i) => (
              <li key={i}>
                <span className="text-muted-foreground">
                  {formatWhen(item.at)} · {MEMORY_LABELS[item.kind] ?? humanize(item.kind)}:
                </span>{" "}
                {item.text}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title="Consent">
        {activity.consents.length ? (
          <ul className="flex flex-col gap-1 text-[12.5px]">
            {activity.consents.map((c) => (
              <li key={c.consentId}>
                {humanize(c.channel)}: <span className="font-semibold">{humanize(c.status)}</span>{" "}
                <span className="text-muted-foreground">
                  ({humanize(c.basis)}, via {humanize(c.source)}, {formatWhen(c.recordedAt)})
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12.5px] text-muted-foreground">No consent recorded — Rebecca won&apos;t call.</p>
        )}
      </Section>

      <Section title="Emails">
        {activity.emails.length ? (
          <ul className="flex flex-col gap-1 text-[12.5px]">
            {activity.emails.map((m) => (
              <li key={m.messageId} className="flex justify-between gap-2">
                <Link href={`/outreach/emails?status=all&email=${encodeURIComponent(m.messageId)}`} className="truncate">
                  {m.direction === "inbound" ? "↩ " : ""}
                  {m.subject || "(no subject)"}
                </Link>
                <span className="shrink-0 text-muted-foreground">{humanize(m.status)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12.5px] text-muted-foreground">No emails.</p>
        )}
      </Section>

      <Section title="Calls">
        {activity.calls.length ? (
          <ul className="flex flex-col gap-1 text-[12.5px]">
            {activity.calls.map((c) => (
              <li key={c.callId} className="flex justify-between gap-2">
                <Link href={`/outreach/calls?call=${encodeURIComponent(c.callId)}`}>
                  {formatWhen(c.createdAt)} · {humanize(c.direction)}
                </Link>
                <span className="text-muted-foreground">
                  {humanize(c.outcome ?? c.status)} · {formatDuration(c.durationS)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12.5px] text-muted-foreground">No calls.</p>
        )}
      </Section>
    </>
  );
}
