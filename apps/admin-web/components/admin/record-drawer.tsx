"use client";

/**
 * RecordDrawer -- SR-17 D3. The ONE shared timeline-shaped drawer used by
 * both the Contact record (`/contacts?contact=<id>`) and the Lead record's
 * Timeline tab (`/leads?lead=<id>&tab=timeline`). Per D3, the only
 * difference between the two call sites is which timeline endpoint supplied
 * the `TimelineResult` -- this component itself has no branch on subject
 * kind beyond the header's secondary identifier and the "View account"
 * link, and is deliberately built to be SR-18's third consumer too.
 *
 * Renders (M8): header (avatar + name + secondary identifier + close), an
 * optional summary callout, a degradation notice (D4, MANDATORY when
 * `degraded` is true), and a vertical dotted-rail timeline of items with a
 * relative timestamp + nested detail card per item. An explicit "Load
 * older" control drives `?before=` (scope item 10) -- no infinite scroll,
 * no client-accumulated state beyond what's already in the URL.
 *
 * URL-driven open/close (D3): the container (`record-drawer-container.tsx`
 * for contacts; `lead-drawer.tsx`'s Timeline tab for leads) reads
 * `?contact=`/`?lead=` server-side and only renders this component when
 * present -- so "closing" is simply navigating without that param. This
 * component owns focus-trap/Escape/focus-restore only, mirroring
 * `lead-drawer.tsx`'s existing hand-rolled trap exactly.
 *
 * D4 (LOCKED, MANDATORY): when `result.degraded` is true, a visible,
 * non-dismissible notice naming which sources failed is rendered ABOVE the
 * timeline items -- never flattened away, never silently shorter-looking.
 */
import { useCallback, useEffect, useRef } from "react";
import Link from "next/link";
import type { TimelineFetchResult, TimelineItem } from "@/lib/timeline";
import { cn } from "@/lib/utils";

export interface RecordDrawerSubject {
  kind: "contact" | "lead";
  id: string;
  /** Header title, e.g. a contact/lead's name. `null`/empty renders a
   * generic fallback rather than an empty header (no-silent-fallback). */
  name: string | null;
  /** Header secondary line, e.g. email (+ phone). */
  secondary: string | null;
  /** Optional summary callout text (M8's "This lead is qualified and
   * assigned to Ava Chen…" callout). Omitted entirely when `null` -- no
   * fabricated summary is ever shown. */
  summary?: string | null;
  /** Contact only: link target for "View account" when the contact has a
   * linked `account_id` (D5: rendered as a link, never a raw id, never
   * resolved via an N+1 fetch). */
  accountId?: string | null;
}

export interface RecordDrawerProps {
  subject: RecordDrawerSubject;
  result: TimelineFetchResult;
  onClose: () => void;
  /** Called with the oldest currently-loaded item's `occurredAt` to build
   * the `?before=` link for "Load older" (scope item 10). `null` when there
   * is nothing older to load (`result.data.nextBefore === null`). */
  loadOlderHref: string | null;
}

function formatRelative(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.round(diffMs / 60000);
  const absolute = date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  if (diffMin < 1) return `${absolute} · just now`;
  if (diffMin < 60) return `${absolute} · ${diffMin} min ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${absolute} · ${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${absolute} · ${diffDay}d ago`;
}

function initials(name: string | null): string {
  const trimmed = (name ?? "").trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/);
  if (parts.length === 1) return trimmed.slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

export function RecordDrawer({ subject, result, onClose, loadOlderHref }: RecordDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<Element | null>(null);

  useEffect(() => {
    triggerRef.current = document.activeElement;
    closeButtonRef.current?.focus();
    return () => {
      if (triggerRef.current instanceof HTMLElement) {
        triggerRef.current.focus();
      }
    };
  }, []);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;

      const panel = panelRef.current;
      if (!panel) return;
      const focusable = panel.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (event.shiftKey) {
        if (active === first || !panel.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !panel.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    },
    [onClose]
  );

  const titleId = `record-drawer-title-${subject.kind}-${subject.id}`;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={panelRef}
      onKeyDown={handleKeyDown}
      className="fixed top-0 right-0 bottom-0 z-50 flex w-full max-w-[410px] flex-col bg-card"
      style={{ boxShadow: "-24px 0 48px rgba(25,26,23,.14)", borderLeft: "1px solid var(--line)" }}
    >
      <div className="flex flex-col gap-3 border-b border-border p-5">
        <div className="flex items-center gap-3">
          <div className="grid size-11 shrink-0 place-items-center rounded-full bg-[var(--foreground)] text-sm font-bold text-white">
            {initials(subject.name)}
          </div>
          <div className="min-w-0 flex-1">
            <p id={titleId} className="truncate text-base font-bold text-foreground">
              {subject.name || (subject.kind === "contact" ? "Unnamed contact" : "Unnamed lead")}
            </p>
            <p className="truncate text-xs text-muted-foreground">{subject.secondary || "—"}</p>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label={`Close ${subject.kind} detail`}
            className="grid size-11 shrink-0 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <span aria-hidden className="text-sm">
              ✕
            </span>
          </button>
        </div>
        {subject.accountId ? (
          <Link
            href="/accounts"
            className="w-fit text-xs font-semibold text-foreground underline underline-offset-2"
            title={`Account ${subject.accountId} — there is no per-account detail page yet (D5); this links to the Accounts list.`}
          >
            View accounts
          </Link>
        ) : null}
      </div>

      {subject.summary ? (
        <div className="mx-5 mt-4 flex gap-2.5 rounded-[10px] bg-[var(--callout-bg)] px-3.5 py-3 text-[12.5px] leading-[1.5] text-[var(--ink-2)]">
          <svg
            aria-hidden
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="#404040"
            strokeWidth="1.8"
            className="mt-0.5 shrink-0"
          >
            <circle cx="12" cy="12" r="9" />
            <path d="M12 8h.01M11 12h1v4h1" />
          </svg>
          <div>{subject.summary}</div>
        </div>
      ) : null}

      <div className="flex-1 overflow-y-auto p-5">
        <RecordDrawerTimelinePanel result={result} loadOlderHref={loadOlderHref} />
      </div>
    </div>
  );
}

/**
 * The timeline error/degradation/items switch, extracted so BOTH the
 * Contact record drawer (above, full dialog chrome) and the Lead record
 * drawer's Timeline tab (`leads/lead-drawer.tsx`, which already owns its
 * own tabbed dialog chrome) render the exact same timeline body -- one
 * implementation of D4's degradation disclosure, not two that could drift.
 * This IS the "shared component" D3 requires; the dialog wrapper above is
 * additionally used for the Contact entry point specifically because
 * contacts have no pre-existing tabbed drawer to slot a tab into.
 */
export function RecordDrawerTimelinePanel({
  result,
  loadOlderHref,
}: {
  result: TimelineFetchResult;
  loadOlderHref: string | null;
}) {
  if (result.status === "error") {
    return (
      <p
        role="alert"
        className="rounded-lg border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]"
      >
        {result.message}
        {result.correlationId ? (
          <span className="mt-1 block text-xs opacity-80">Correlation ID: {result.correlationId}</span>
        ) : null}
      </p>
    );
  }
  return <TimelineBody result={result} loadOlderHref={loadOlderHref} />;
}

function TimelineBody({
  result,
  loadOlderHref,
}: {
  result: Extract<TimelineFetchResult, { status: "ok" }>;
  loadOlderHref: string | null;
}) {
  const { data } = result;
  const failedSources = Object.entries(data.sources).filter(([, s]) => s.state !== "ok");

  return (
    <div className="flex flex-col gap-4">
      {/* D4 MANDATORY: a visible, non-dismissible notice naming which
          sources failed, rendered ABOVE the items -- never omitted, never
          coerced away, and never merely a shorter-looking timeline. */}
      {data.degraded ? (
        <div
          role="alert"
          aria-live="polite"
          data-testid="timeline-degraded-notice"
          className="rounded-lg border border-[#f0d9a8] bg-[#fdf6e3] p-3 text-[12.5px] text-[#8a6416]"
        >
          <p className="font-semibold">This history may be incomplete.</p>
          <p className="mt-0.5">
            {failedSources.length > 0
              ? `Could not load: ${failedSources.map(([name]) => name).join(", ")}.`
              : "One or more data sources could not be loaded."}
          </p>
        </div>
      ) : null}

      {data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No activity yet.</p>
      ) : (
        <ol className="flex flex-col gap-0" aria-label="Timeline">
          {data.items.map((item, index) => (
            <TimelineRow
              key={`${item.kind}-${item.id}`}
              item={item}
              index={index}
              isLast={index === data.items.length - 1}
            />
          ))}
        </ol>
      )}

      {loadOlderHref ? (
        <Link
          href={loadOlderHref}
          scroll={false}
          className="self-start text-xs font-semibold text-foreground underline underline-offset-2"
        >
          Load older
        </Link>
      ) : null}
    </div>
  );
}

/** SR-24: recency-graded rail dot -- the data carries no per-item
 * status/severity field, so color is keyed off list position (newest first),
 * matching the reference's example (green/grey/light-grey), not a fabricated
 * status taxonomy. */
function railDotClass(index: number): string {
  if (index === 0) return "bg-[var(--success-dot)]";
  if (index === 1) return "bg-muted-foreground";
  return "bg-border";
}

function capitalize(value: string): string {
  return value.length === 0 ? value : value[0].toUpperCase() + value.slice(1);
}

/** SR-24: human title per timeline item, replacing the raw `item.kind`/
 * `data.type` strings that were leaking into the UI. Unknown kinds/sub-types
 * fall back to the raw string -- honest degradation, not a thrown error. */
function timelineItemLabel(item: TimelineItem): string {
  const data = item.data ?? {};
  if (item.kind === "lead_activity") {
    const type = typeof data.type === "string" ? data.type : "";
    switch (type) {
      case "stage_change": {
        const to = typeof data.to_stage === "string" ? data.to_stage : undefined;
        const payload = (data.payload ?? {}) as Record<string, unknown>;
        const toStage = to ?? (typeof payload.to_stage === "string" ? payload.to_stage : "?");
        return `Stage changed to ${toStage}`;
      }
      case "note":
        return "Note added";
      case "assignment":
        return "Lead assigned";
      case "converted_to_contact":
        return "Converted to contact";
      default:
        return type || "lead_activity";
    }
  }
  if (item.kind === "booking") return "Meeting booked";
  if (item.kind === "notification") {
    const channel = typeof data.channel === "string" ? data.channel : "";
    return channel ? `${capitalize(channel)} sent` : "Notification sent";
  }
  if (item.kind === "message") return "Conversation message";
  return item.kind;
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/**
 * SR-24: per-kind `.tbl-card` body -- Owner / Signals / Summary rows built
 * ONLY from fields the backend actually sends (`services/api/src/api/
 * timeline/service.py`), replacing the previous generic `Object.entries`
 * dump (which leaked raw snake_case keys and `JSON.stringify` output).
 * Unknown kinds keep the old generic rendering as an honest fallback.
 */
function TimelineEventCard({ item }: { item: TimelineItem }) {
  const data = item.data ?? {};

  if (item.kind === "lead_activity") {
    const type = typeof data.type === "string" ? data.type : "";
    const actor = typeof data.actor === "string" ? data.actor : null;
    const payload = (data.payload ?? {}) as Record<string, unknown>;
    let summary: string | null = null;
    if (type === "note") {
      summary = typeof payload.text === "string" ? payload.text : null;
    } else if (type === "stage_change") {
      const from = typeof payload.from_stage === "string" ? payload.from_stage : "?";
      const to = typeof payload.to_stage === "string" ? payload.to_stage : "?";
      summary = `Moved from ${from} to ${to}`;
    } else if (type === "assignment") {
      const agentId = typeof payload.agent_id === "string" ? payload.agent_id : null;
      summary = agentId ? `Assigned to ${agentId}` : "Unassigned";
    }
    return (
      <TbLCard>
        {actor ? <OwnerRow actor={actor} /> : null}
        <SignalsRow chips={type ? [type] : []} />
        <SummaryText text={summary} />
      </TbLCard>
    );
  }

  if (item.kind === "booking") {
    const source = typeof data.source === "string" ? data.source : null;
    const status = typeof data.status === "string" ? data.status : null;
    const startsAt = typeof data.starts_at === "string" ? data.starts_at : null;
    return (
      <TbLCard>
        <SignalsRow chips={[source, status].filter((v): v is string => Boolean(v))} />
        <SummaryText text={startsAt ? `Scheduled for ${formatDateTime(startsAt)}` : null} />
      </TbLCard>
    );
  }

  if (item.kind === "notification") {
    const channel = typeof data.channel === "string" ? data.channel : null;
    const status = typeof data.status === "string" ? data.status : null;
    const subject = typeof data.subject === "string" ? data.subject : null;
    return (
      <TbLCard>
        <SignalsRow chips={[channel, status].filter((v): v is string => Boolean(v))} />
        <SummaryText text={subject} />
      </TbLCard>
    );
  }

  if (item.kind === "message") {
    const role = typeof data.role === "string" ? data.role : null;
    const truncated = data.truncated === true;
    const content = typeof data.content === "string" ? data.content : null;
    const conversationId = typeof data.conversation_id === "string" ? data.conversation_id : null;
    return (
      <TbLCard>
        <SignalsRow chips={[role, truncated ? "Truncated" : null].filter((v): v is string => Boolean(v))} />
        <SummaryText text={content} clamp />
        {conversationId ? (
          <Link
            href={`/conversations?conversation=${encodeURIComponent(conversationId)}`}
            className="mt-3 flex min-h-8 w-full items-center justify-center rounded-[9px] border border-border bg-card px-3 text-[12.5px] font-semibold text-[var(--ink-2)] transition-colors hover:bg-secondary"
          >
            View conversation ↗
          </Link>
        ) : null}
      </TbLCard>
    );
  }

  // Unknown kind: fall back to the original generic key/value dump.
  const detailEntries = Object.entries(data);
  if (detailEntries.length === 0) return null;
  return (
    <TbLCard>
      {detailEntries.map(([key, value]) => (
        <div key={key} className="flex items-center justify-between gap-3 text-[12.5px]">
          <span className="text-muted-foreground">{key}</span>
          <span className="truncate font-medium text-[var(--ink-2)]">
            {typeof value === "string" || typeof value === "number" || typeof value === "boolean"
              ? String(value)
              : value === null
                ? "—"
                : JSON.stringify(value)}
          </span>
        </div>
      ))}
    </TbLCard>
  );
}

function TbLCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-3 rounded-[12px] border border-border bg-card p-[14px]">
      {children}
    </div>
  );
}

function OwnerRow({ actor }: { actor: string }) {
  return (
    <div className="mb-[9px] flex items-center justify-between text-[12.5px]">
      <span className="text-muted-foreground">Owner</span>
      <span className="flex items-center gap-[7px] font-semibold text-[var(--ink-2)]">
        <span className="grid size-[22px] shrink-0 place-items-center rounded-full bg-secondary text-[9px] font-bold text-[var(--ink-2)]">
          {initials(actor)}
        </span>
        {actor}
      </span>
    </div>
  );
}

function SignalsRow({ chips }: { chips: string[] }) {
  if (chips.length === 0) return null;
  return (
    <div className="mb-[11px] flex items-center justify-between gap-2 text-[12.5px]">
      <span className="text-muted-foreground">Signals</span>
      <span className="flex flex-wrap justify-end gap-1.5">
        {chips.map((label) => (
          <span
            key={label}
            className="inline-flex h-[22px] items-center rounded-[7px] bg-secondary px-2.5 text-[11.5px] font-semibold text-[var(--ink-2)]"
          >
            {label}
          </span>
        ))}
      </span>
    </div>
  );
}

function SummaryText({ text, clamp }: { text: string | null; clamp?: boolean }) {
  if (!text) return null;
  return (
    <div>
      <p className="mb-[2px] text-[12.5px] text-muted-foreground">Summary</p>
      <p className={cn("text-[12.5px] leading-[1.5]", clamp && "line-clamp-3")}>{text}</p>
    </div>
  );
}

function TimelineRow({ item, index, isLast }: { item: TimelineItem; index: number; isLast: boolean }) {
  return (
    <li className="relative flex gap-[14px]">
      <div className="flex flex-col items-center">
        <span aria-hidden className={cn("mt-1 size-[11px] shrink-0 rounded-full", railDotClass(index))} />
        {isLast ? null : <span aria-hidden className="mt-1 w-[1.5px] flex-1 bg-[var(--row-line)]" />}
      </div>
      <div className={cn("min-w-0 flex-1", isLast ? "pb-0" : "pb-[22px]")}>
        <p className="text-[14px] font-semibold text-foreground">{timelineItemLabel(item)}</p>
        <p className="mt-0.5 text-[11.5px] text-muted-foreground">{formatRelative(item.occurredAt)}</p>
        <TimelineEventCard item={item} />
      </div>
    </li>
  );
}
