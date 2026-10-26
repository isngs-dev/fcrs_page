/**
 * Emails (A8 U4): Rebecca's outbound drafts/sends and the replies she received.
 * `?status=` filters (default Drafts), `?email=` opens the detail panel. Drafts
 * can be edited, approved (the compliance gate re-runs server-side) or discarded.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { approveEmail, discardEmail, getEmail, listEmails, type EmailMessage } from "@/lib/outreach";
import { BAND_LABELS, EMAIL_FILTERS, formatWhen, humanize, isEmailFilter } from "@/lib/outreach-format";
import { SoftCard } from "@/components/admin/soft-card";
import { Chip } from "@/components/admin/chip";
import { TableCard, TableCell, TableHeadCell, TableRow } from "@/components/admin/table-card";
import { EmptyCard, ErrorCard, FilterPills, OutreachHeader } from "@/app/(protected)/outreach/outreach-ui";
import { ActionButton } from "@/app/(protected)/outreach/action-button";
import { DraftEditor } from "@/app/(protected)/outreach/emails/draft-editor";

type SearchParams = Promise<{ [key: string]: string | string[] | undefined }>;

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function href(status: string | undefined, email?: string): string {
  const params = new URLSearchParams();
  params.set("status", status ?? "all");
  if (email) params.set("email", email);
  return `/outreach/emails?${params}`;
}

function EmailDetail({ email, status }: { email: EmailMessage; status: string | undefined }) {
  const isDraft = email.status === "draft";
  return (
    <SoftCard className="flex flex-col gap-4 px-5 py-5">
      <div className="flex flex-wrap items-center gap-2">
        <Chip>{humanize(email.status)}</Chip>
        <Chip>{email.direction === "inbound" ? "Received" : "Outbound"}</Chip>
        {email.band ? <Chip>{BAND_LABELS[email.band] ?? humanize(email.band)}</Chip> : null}
        {email.classification ? <Chip>Reply type: {humanize(email.classification)}</Chip> : null}
        {email.handledAction ? <Chip>Action: {humanize(email.handledAction)}</Chip> : null}
        <Link href={href(status)} className="ml-auto text-[12.5px]">
          Close
        </Link>
      </div>
      <dl className="grid grid-cols-[80px_1fr] gap-y-1 text-[13px]">
        <dt className="text-muted-foreground">From</dt>
        <dd>{email.from}</dd>
        <dt className="text-muted-foreground">To</dt>
        <dd>{email.to}</dd>
        <dt className="text-muted-foreground">Created</dt>
        <dd>{formatWhen(email.createdAt)}</dd>
        {email.sentAt ? (
          <>
            <dt className="text-muted-foreground">Sent</dt>
            <dd>{formatWhen(email.sentAt)}</dd>
          </>
        ) : null}
      </dl>
      {email.lastError ? (
        <p role="alert" className="text-[12.5px] text-destructive">
          {email.lastError}
        </p>
      ) : null}

      {isDraft ? (
        <>
          <DraftEditor messageId={email.messageId} subject={email.subject} bodyText={email.bodyText ?? ""} />
          <div className="flex flex-wrap gap-2 border-t border-[var(--row-line)] pt-4">
            <ActionButton
              action={approveEmail.bind(null, email.messageId)}
              label="Approve & send"
              pendingLabel="Sending…"
              confirmText="Send this email now?"
            />
            <ActionButton
              action={discardEmail.bind(null, email.messageId)}
              label="Discard"
              variant="destructive"
              confirmText="Discard this draft? It won't be sent."
              doneHref={href(status)}
            />
          </div>
          <p className="text-[11.5px] text-muted-foreground">
            Save your edits before approving. Sending re-checks consent, opt-outs and limits.
          </p>
        </>
      ) : (
        <>
          <h2 className="text-[16px] font-semibold">{email.subject}</h2>
          <pre className="whitespace-pre-wrap font-sans text-[13px] leading-[1.55] text-foreground">{email.bodyText}</pre>
        </>
      )}

      {email.sources && email.sources.length ? (
        <div className="flex flex-col gap-1 border-t border-[var(--row-line)] pt-3">
          <p className="text-[12px] font-semibold text-muted-foreground">Answer sources</p>
          <ul className="list-disc pl-5 text-[12.5px]">
            {email.sources.map((s, i) => (
              <li key={i}>{s.title ?? s.docId ?? "Knowledge base"}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </SoftCard>
  );
}

export default async function OutreachEmailsPage({ searchParams }: { searchParams: SearchParams }) {
  await requireAnyRole("CLIENT_ADMIN", "CLIENT_AGENT");
  const params = await searchParams;
  const rawStatus = first(params.status);
  const status = rawStatus === "all" ? undefined : isEmailFilter(rawStatus) ? rawStatus : "draft";
  const selected = first(params.email);

  const [list, detail] = await Promise.all([listEmails(status), selected ? getEmail(selected) : null]);

  return (
    <div className="flex flex-col gap-5">
      <OutreachHeader title="Emails" subtitle="Rebecca's drafts, sent follow-ups and the replies she received." />
      <FilterPills active={status} pills={EMAIL_FILTERS.map((f) => ({ label: f.label, value: f.status, href: href(f.status) }))} />

      <div className={selected ? "grid gap-4 lg:grid-cols-[1fr_1.1fr]" : ""}>
        {list.status === "error" ? (
          <ErrorCard message={list.message} correlationId={list.correlationId} />
        ) : list.emails.length === 0 ? (
          <EmptyCard
            title="No emails here"
            body={status === "draft" ? "Drafts waiting for review will show up here." : "Nothing matches this filter yet."}
          />
        ) : (
          <TableCard className="overflow-x-auto">
            <thead>
              <tr>
                <TableHeadCell>Subject</TableHeadCell>
                <TableHeadCell>{status === "received" ? "From" : "To"}</TableHeadCell>
                <TableHeadCell>Status</TableHeadCell>
                <TableHeadCell>When</TableHeadCell>
              </tr>
            </thead>
            <tbody>
              {list.emails.map((m) => (
                <TableRow key={m.messageId} className={m.messageId === selected ? "bg-secondary" : undefined}>
                  <TableCell>
                    <Link href={href(status, m.messageId)} className="font-semibold">
                      {m.subject || "(no subject)"}
                    </Link>
                  </TableCell>
                  <TableCell>{m.direction === "inbound" ? m.from : m.to}</TableCell>
                  <TableCell>{humanize(m.status)}</TableCell>
                  <TableCell>{formatWhen(m.sentAt ?? m.createdAt)}</TableCell>
                </TableRow>
              ))}
            </tbody>
          </TableCard>
        )}

        {detail ? (
          detail.status === "error" ? (
            <ErrorCard message={detail.message} correlationId={detail.correlationId} />
          ) : (
            <EmailDetail email={detail.email} status={status} />
          )
        ) : null}
      </div>
    </div>
  );
}
