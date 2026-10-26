/**
 * Calls (A8 U5): every call Rebecca placed or answered; `?call=` opens the
 * summary, the transcript turn by turn and the recording (played through our
 * own proxy, U6 -- never a Plivo URL in the browser).
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { getCall, listCalls, type CallDetail } from "@/lib/outreach";
import { formatDuration, formatWhen, humanize } from "@/lib/outreach-format";
import { SoftCard } from "@/components/admin/soft-card";
import { Chip } from "@/components/admin/chip";
import { TableCard, TableCell, TableHeadCell, TableRow } from "@/components/admin/table-card";
import { EmptyCard, ErrorCard, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";

type SearchParams = Promise<{ [key: string]: string | string[] | undefined }>;

const SPEAKER: Record<string, string> = { rebecca: "Rebecca", caller: "Caller", system: "System" };

/** The customer's number: the one we dialled (outbound) or the caller (inbound). */
function customer(call: { direction: string; fromE164: string; toE164: string }): string {
  return call.direction === "inbound" ? call.fromE164 : call.toE164;
}

function CallPanel({ call }: { call: CallDetail }) {
  return (
    <SoftCard className="flex flex-col gap-4 px-5 py-5">
      <div className="flex flex-wrap items-center gap-2">
        <Chip>{call.direction === "inbound" ? "Inbound" : "Outbound"}</Chip>
        <Chip>{humanize(call.status)}</Chip>
        {call.outcome ? <Chip tone={call.outcome === "booked" ? "success" : "default"}>{humanize(call.outcome)}</Chip> : null}
        <Link href="/outreach/calls" className="ml-auto text-[12.5px]">
          Close
        </Link>
      </div>
      <dl className="grid grid-cols-[110px_1fr] gap-y-1 text-[13px]">
        <dt className="text-muted-foreground">Number</dt>
        <dd>{customer(call)}</dd>
        <dt className="text-muted-foreground">When</dt>
        <dd>{formatWhen(call.createdAt)}</dd>
        <dt className="text-muted-foreground">Duration</dt>
        <dd>{formatDuration(call.durationS)}</dd>
        {call.transferReason ? (
          <>
            <dt className="text-muted-foreground">Handed over</dt>
            <dd>{humanize(call.transferReason)}</dd>
          </>
        ) : null}
      </dl>
      {call.summary ? (
        <div className="rounded-[10px] bg-secondary px-4 py-3 text-[13px]">
          <p className="mb-1 text-[12px] font-semibold text-muted-foreground">Summary</p>
          {call.summary}
        </div>
      ) : null}

      <div className="flex flex-col gap-1.5">
        <p className="text-[12px] font-semibold text-muted-foreground">Recording</p>
        {call.hasRecording ? (
          <audio controls preload="none" src={`/outreach/calls/${encodeURIComponent(call.callId)}/recording`} className="w-full">
            <track kind="captions" />
          </audio>
        ) : (
          <p className="text-[12.5px] text-muted-foreground">
            {call.recordingConsent ? "No recording for this call." : "Not recorded — the caller asked us not to."}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <p className="text-[12px] font-semibold text-muted-foreground">Transcript</p>
        {call.transcript.length === 0 ? (
          <p className="text-[12.5px] text-muted-foreground">Nothing was said on this call.</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {call.transcript.map((t) => (
              <li
                key={t.turnIndex}
                className={
                  "max-w-[85%] rounded-[10px] px-3 py-2 text-[13px] " +
                  (t.speaker === "caller" ? "self-end bg-secondary" : "self-start border border-[var(--line)]")
                }
              >
                <span className="block text-[11px] font-semibold text-muted-foreground">{SPEAKER[t.speaker] ?? t.speaker}</span>
                {t.text}
              </li>
            ))}
          </ol>
        )}
      </div>
    </SoftCard>
  );
}

export default async function OutreachCallsPage({ searchParams }: { searchParams: SearchParams }) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");
  const params = await searchParams;
  const selected = Array.isArray(params.call) ? params.call[0] : params.call;
  const [list, detail] = await Promise.all([listCalls(), selected ? getCall(selected) : null]);

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Calls" subtitle="Calls Rebecca placed and answered, with transcripts and recordings." />
      <div className={selected ? "grid gap-4 lg:grid-cols-[1fr_1.1fr]" : ""}>
        {list.status === "error" ? (
          <ErrorCard message={list.message} correlationId={list.correlationId} />
        ) : list.calls.length === 0 ? (
          <EmptyCard title="No calls yet" body="Calls show up here once the calling agent is switched on." />
        ) : (
          <TableCard className="overflow-x-auto">
            <thead>
              <tr>
                <TableHeadCell>Number</TableHeadCell>
                <TableHeadCell>Direction</TableHeadCell>
                <TableHeadCell>Outcome</TableHeadCell>
                <TableHeadCell>Duration</TableHeadCell>
                <TableHeadCell>When</TableHeadCell>
              </tr>
            </thead>
            <tbody>
              {list.calls.map((c) => (
                <TableRow key={c.callId} className={c.callId === selected ? "bg-secondary" : undefined}>
                  <TableCell>
                    <Link href={`/outreach/calls?call=${encodeURIComponent(c.callId)}`} className="font-semibold">
                      {customer(c)}
                    </Link>
                  </TableCell>
                  <TableCell>{humanize(c.direction)}</TableCell>
                  <TableCell>{humanize(c.outcome ?? c.status)}</TableCell>
                  <TableCell>{formatDuration(c.durationS)}</TableCell>
                  <TableCell>{formatWhen(c.createdAt)}</TableCell>
                </TableRow>
              ))}
            </tbody>
          </TableCard>
        )}
        {detail ? (
          detail.status === "error" ? (
            <ErrorCard message={detail.message} correlationId={detail.correlationId} />
          ) : (
            <CallPanel call={detail.call} />
          )
        ) : null}
      </div>
    </div>
  );
}
