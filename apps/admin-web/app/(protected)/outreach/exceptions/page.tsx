/**
 * Exceptions (A13 R4, CLIENT_ADMIN): everything that failed, was blocked or needs a
 * person -- one place, with Retry / Dismiss where that makes sense.
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { dismissTask, getExceptions, retryEvent, retryTask } from "@/lib/outreach";
import { formatWhen, humanize } from "@/lib/outreach-format";
import { SoftCard } from "@/components/admin/soft-card";
import { Chip } from "@/components/admin/chip";
import { EmptyCard, ErrorCard, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";
import { ActionButton } from "@/app/(protected)/outreach/action-button";

/** Plain words for the codes an admin is most likely to see here. */
const CODES: Record<string, string> = {
  NO_CONSENT: "No permission for that channel",
  CONTACT_SUPPRESSED: "On the do-not-contact list",
  OUTSIDE_CALLING_WINDOW: "Outside calling hours",
  OUTSIDE_EMAIL_WINDOW: "Outside email hours",
  CONTACT_TIMEZONE_UNKNOWN: "Their timezone is unknown",
  CADENCE_LIMIT: "Too soon after the last contact",
  MAX_TOUCHES: "Contact limit reached",
  COST_CAP_REACHED: "Monthly cost cap reached",
  NO_ANSWER_FOUND: "The knowledge base didn't have the answer",
  UNSTATED_COMMITMENT: "Would have promised something not in the knowledge base",
  LLM_ERROR: "The AI model was unavailable",
};

const label = (code: string | null) => (code ? CODES[code] ?? humanize(code) : "—");

export default async function ExceptionsPage() {
  await requireRole("CLIENT_ADMIN");
  const result = await getExceptions();

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Exceptions" subtitle="What failed, what was blocked, and what needs a person." />
      {result.status === "error" ? (
        <ErrorCard message={result.message} correlationId={result.correlationId} />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <SoftCard className="px-5 py-4 text-[13px]">
              <p className="font-semibold">Handed to your team (7 days): {result.exceptions.needsHuman7d}</p>
              <Link href="/notifications?category=leads">Open notifications →</Link>
            </SoftCard>
            <SoftCard className="px-5 py-4 text-[13px]">
              <p className="font-semibold">Waiting in the intake queue: {result.exceptions.intakeQueue}</p>
              <Link href="/outreach/intake">Open lead intake →</Link>
            </SoftCard>
          </div>

          <SoftCard className="px-5 py-4">
            <h2 className="pb-2 text-[16px] font-semibold">Blocked by the safety checks (7 days)</h2>
            {Object.keys(result.exceptions.blocked7d).length === 0 ? (
              <p className="text-[12.5px] text-muted-foreground">Nothing was blocked.</p>
            ) : (
              <ul className="flex flex-wrap gap-2">
                {Object.entries(result.exceptions.blocked7d).map(([code, n]) => (
                  <li key={code}>
                    <Chip>
                      {label(code)}: {n}
                    </Chip>
                  </li>
                ))}
              </ul>
            )}
            <p className="pt-2 text-[11.5px] text-muted-foreground">
              Most blocks are expected (hours, limits); they wait and retry on their own.
            </p>
          </SoftCard>

          <SoftCard className="px-5 py-4">
            <h2 className="pb-2 text-[16px] font-semibold">Steps that failed or were escalated</h2>
            {result.exceptions.tasks.length === 0 ? (
              <EmptyCard title="All clear" body="No failed or escalated steps." />
            ) : (
              <ul>
                {result.exceptions.tasks.map((t) => (
                  <li key={t.taskId} className="flex flex-wrap items-center gap-3 border-b border-[var(--row-line)] py-2 last:border-b-0">
                    <span className="min-w-0 flex-1 text-[13px]">
                      <span className="font-semibold">{t.name ?? "Contact"}</span>
                      <span className="text-muted-foreground">
                        {" "}
                        · {humanize(t.channel)} {t.status} · {label(t.lastError)} · {formatWhen(t.scheduledAt)}
                      </span>
                    </span>
                    {t.planStatus === "active" ? (
                      <ActionButton action={retryTask.bind(null, t.taskId)} label="Retry" variant="outline" />
                    ) : null}
                    <ActionButton action={dismissTask.bind(null, t.taskId)} label="Dismiss" variant="ghost" />
                  </li>
                ))}
              </ul>
            )}
          </SoftCard>

          <SoftCard className="px-5 py-4">
            <h2 className="pb-2 text-[16px] font-semibold">Background jobs that gave up</h2>
            {result.exceptions.deadEvents.length === 0 ? (
              <p className="text-[12.5px] text-muted-foreground">None.</p>
            ) : (
              <ul>
                {result.exceptions.deadEvents.map((e) => (
                  <li key={e.eventId} className="flex flex-wrap items-center gap-3 border-b border-[var(--row-line)] py-2 last:border-b-0">
                    <span className="min-w-0 flex-1 text-[13px]">
                      <span className="font-semibold">{humanize(e.kind)}</span>
                      <span className="text-muted-foreground">
                        {" "}
                        · {e.attempts} tries · {label(e.lastError)} · {formatWhen(e.createdAt)}
                      </span>
                    </span>
                    <ActionButton action={retryEvent.bind(null, e.eventId)} label="Retry" variant="outline" />
                  </li>
                ))}
              </ul>
            )}
          </SoftCard>
        </>
      )}
    </div>
  );
}
