"use client";

/**
 * SR-24: right-side lead detail drawer. 410px, shadow
 * `-24px 0 48px rgba(25,26,23,.14)`, tabs Timeline/Details/Notes with the
 * `.seg` pill control (matches the Table/Board toggle recipe) -- reduced
 * from the previous 5-tab strip.
 *
 * SR-24 (supersedes SR-22 scope item 4's "keep 5 tabs" reasoning): the
 * Transcript tab is DELETED -- `Lead` has no `conversation_id` column
 * (`services/api/src/api/leads/repository.py`), so it could only ever render
 * a permanent "not available" state; a tab that can never contain data is
 * dead chrome, not preserved functionality. The Activity tab is DELETED --
 * the Timeline endpoint's `lead_activity` items are the exact same
 * `list_activities()` rows Activity showed, just merged with
 * bookings/notifications/messages, so Activity was a strict subset of
 * Timeline and removing it loses nothing. Details and Notes remain: Notes is
 * a live server-action form (real, irreplaceable functionality); Details
 * holds the structured field list (stage/score/source/assigned) that used to
 * sit in a header badge row with no reference equivalent -- moved here.
 *
 * Timeline is now the DEFAULT tab (was Transcript) -- opening a lead now
 * shows the reference's summary-callout + dotted-rail shape immediately,
 * with no tab interaction required, matching the design reference exactly.
 *
 * State lives in the URL (`?lead=<id>&tab=<tab>`) so the drawer is
 * linkable/shareable -- the same pattern `leads-filter.tsx` uses for the
 * stage filter, just router-driven instead of a `<form method="get">` since
 * this needs client-side Esc/focus handling a plain form navigation can't
 * provide.
 *
 * Data: `page.tsx` fetches the lead detail + activities server-side (this
 * repo's server-first convention) and passes them down as props -- this
 * component only renders what it's given plus owns tab/open/focus state.
 *
 * Keyboard/focus (mirrors apps/widget/src/ui/ChatWidget.tsx's S14.5 pattern):
 * focus moves into the panel on open, Escape closes, a hand-rolled focus
 * trap keeps Tab/Shift+Tab cycling within the panel, and focus restores to
 * the row/trigger that opened it on close.
 */
import { useActionState, useCallback, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import type { LeadDetail, LeadDetailResult, LeadActivitiesResult } from "@/lib/leads-presentation";
import { initialsFromName } from "@/lib/leads-presentation";
import { addLeadNote, type AddNoteState } from "@/app/(protected)/leads/actions";
import type { TimelineFetchResult } from "@/lib/timeline";
import { RecordDrawerTimelinePanel } from "@/components/admin/record-drawer";
import { cn } from "@/lib/utils";
import { TABS, type Tab } from "@/app/(protected)/leads/lead-drawer-tabs";

const TAB_LABELS: Record<Tab, string> = {
  timeline: "Timeline",
  details: "Details",
  notes: "Notes",
};

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

const initialNoteState: AddNoteState = { status: "idle" };

/**
 * SR-24: composes the reference's "This lead is qualified and assigned to
 * Ava Chen…" callout sentence from REAL `LeadDetail` fields only -- no
 * "Next step" clause, since no backing field exists for one.
 */
function buildSummary(lead: LeadDetail): string {
  const assignment = lead.assignedAgentId ? `assigned to ${lead.assignedAgentId}` : "unassigned";
  return `This lead is ${lead.stage} and ${assignment}. Captured via ${lead.source}.`;
}

interface LeadDrawerProps {
  leadId: string;
  tab: Tab;
  detailResult: LeadDetailResult;
  activitiesResult: LeadActivitiesResult | null;
  /** SR-17 D3/scope item 9: the lead's timeline, fetched only when
   * `tab === "timeline"`; `null` otherwise. Rendered via the SAME
   * `RecordDrawerTimelinePanel` the Contact record drawer uses. */
  timelineResult: TimelineFetchResult | null;
  /** Base path (`/leads` or `/clients/{tenantId}/leads`) the drawer's URL
   * params are read/written against -- mirrors `leads-filter.tsx`'s
   * `basePath` convention for the S13.7 platform-admin tenant-scoped view. */
  basePath: string;
  tenantId?: string;
}

export function LeadDrawer({
  leadId,
  tab,
  detailResult,
  activitiesResult,
  timelineResult,
  basePath,
  tenantId,
}: LeadDrawerProps) {
  const router = useRouter();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  const navigate = useCallback(
    (nextLeadId: string | null, nextTab: Tab) => {
      const params = new URLSearchParams();
      if (nextLeadId) {
        params.set("lead", nextLeadId);
        if (nextTab !== "timeline") params.set("tab", nextTab);
      }
      const qs = params.toString();
      router.push(qs ? `${basePath}?${qs}` : basePath, { scroll: false });
    },
    [router, basePath]
  );

  const close = useCallback(() => navigate(null, "timeline"), [navigate]);

  // Focus-in on open + focus-restore on close (mirrors ChatWidget.tsx).
  const triggerRef = useRef<Element | null>(null);
  useEffect(() => {
    triggerRef.current = document.activeElement;
    closeButtonRef.current?.focus();
    return () => {
      if (triggerRef.current instanceof HTMLElement) {
        triggerRef.current.focus();
      }
    };
    // Mount/unmount only -- this effect intentionally does not depend on
    // leadId so switching leads while open doesn't re-trigger focus jumps.
  }, []);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
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
    [close]
  );

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="lead-drawer-title"
      ref={panelRef}
      onKeyDown={handleKeyDown}
      className="fixed top-0 right-0 bottom-0 z-50 flex w-full max-w-[410px] flex-col bg-card"
      style={{ boxShadow: "-24px 0 48px rgba(25,26,23,.14)", borderLeft: "1px solid var(--row-line)" }}
    >
      {detailResult.status === "error" ? (
        <DrawerErrorState message={detailResult.message} correlationId={detailResult.correlationId} onClose={close} closeButtonRef={closeButtonRef} />
      ) : (
        <DrawerBody
          lead={detailResult.lead}
          tab={tab}
          activitiesResult={activitiesResult}
          timelineResult={timelineResult}
          onClose={close}
          onTabChange={(nextTab) => navigate(leadId, nextTab)}
          closeButtonRef={closeButtonRef}
          tenantId={tenantId}
          leadId={leadId}
          basePath={basePath}
        />
      )}
    </div>
  );
}

function DrawerErrorState({
  message,
  correlationId,
  onClose,
  closeButtonRef,
}: {
  message: string;
  correlationId: string;
  onClose: () => void;
  closeButtonRef: React.RefObject<HTMLButtonElement | null>;
}) {
  return (
    <div className="flex flex-1 flex-col gap-4 p-6">
      <div className="flex items-center justify-between">
        <span id="lead-drawer-title" className="text-base font-bold text-[var(--foreground)]">
          Lead unavailable
        </span>
        <CloseButton onClose={onClose} closeButtonRef={closeButtonRef} />
      </div>
      <p role="alert" className="rounded-lg border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]">
        {message}
        {correlationId ? <span className="mt-1 block text-xs opacity-80">Correlation ID: {correlationId}</span> : null}
      </p>
    </div>
  );
}

function CloseButton({
  onClose,
  closeButtonRef,
}: {
  onClose: () => void;
  closeButtonRef: React.RefObject<HTMLButtonElement | null>;
}) {
  return (
    <button
      ref={closeButtonRef}
      type="button"
      onClick={onClose}
      aria-label="Close lead detail"
      className="grid size-11 shrink-0 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
    >
      <span aria-hidden className="text-sm">
        ✕
      </span>
    </button>
  );
}

function DrawerBody({
  lead,
  tab,
  activitiesResult,
  timelineResult,
  onClose,
  onTabChange,
  closeButtonRef,
  tenantId,
  leadId,
  basePath,
}: {
  lead: LeadDetail;
  tab: Tab;
  activitiesResult: LeadActivitiesResult | null;
  timelineResult: TimelineFetchResult | null;
  onClose: () => void;
  onTabChange: (tab: Tab) => void;
  closeButtonRef: React.RefObject<HTMLButtonElement | null>;
  tenantId?: string;
  leadId: string;
  basePath: string;
}) {
  const noteCount =
    activitiesResult?.status === "ok"
      ? activitiesResult.items.filter((activity) => activity.type === "note").length
      : 0;

  return (
    <>
      <div className="flex flex-col gap-3 border-b border-[var(--row-line)] px-[22px] py-[18px]">
        <div className="flex items-center gap-[11px]">
          <div className="grid size-[34px] shrink-0 place-items-center rounded-full bg-secondary text-[12px] font-semibold text-[var(--ink-2)]">
            {initialsFromName(lead.name)}
          </div>
          <div className="min-w-0 flex-1">
            <p id="lead-drawer-title" className="truncate text-[15px] font-semibold text-foreground">
              {lead.name}
            </p>
            <p className="truncate text-[12px] text-muted-foreground">
              {lead.email}
              {lead.phone ? ` · ${lead.phone}` : ""}
            </p>
          </div>
          <CloseButton onClose={onClose} closeButtonRef={closeButtonRef} />
        </div>

        <div className="flex gap-0.5 self-start rounded-[10px] bg-secondary p-[3px]" role="tablist" aria-label="Lead detail sections">
          {TABS.map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              id={`lead-tab-${t}`}
              aria-selected={tab === t}
              aria-controls={`lead-tabpanel-${t}`}
              onClick={() => onTabChange(t)}
              className={cn(
                "flex h-8 items-center rounded-lg px-4 text-[13px] font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
                tab === t ? "bg-[#333] text-[#fbfaf7]" : "text-muted-foreground hover:text-foreground"
              )}
            >
              {TAB_LABELS[t]}
              {t === "notes" && noteCount > 0 ? (
                <span className="ml-1.5 rounded-full bg-card px-1.5 py-0.5 text-[10px] font-bold text-[var(--ink-2)]">
                  {noteCount}
                </span>
              ) : null}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {tab === "details" ? (
          <DetailsTab lead={lead} />
        ) : tab === "timeline" ? (
          <TimelineTab leadId={leadId} lead={lead} timelineResult={timelineResult} basePath={basePath} />
        ) : (
          <NotesTab
            activitiesResult={activitiesResult}
            leadId={leadId}
            tenantId={tenantId}
            basePath={basePath}
          />
        )}
      </div>
    </>
  );
}

function DetailsTab({ lead }: { lead: LeadDetail }) {
  const rows: Array<[string, string]> = [
    ["Email", lead.email],
    ["Phone", lead.phone ?? "—"],
    ["Status", lead.status],
    ["Stage", lead.stage],
    ["Score", lead.qualificationScore !== null ? String(lead.qualificationScore) : "—"],
    ["Source", lead.source],
    ["Assigned agent", lead.assignedAgentId ?? "— Unassigned"],
  ];
  return (
    <dl id="lead-tabpanel-details" role="tabpanel" aria-labelledby="lead-tab-details" className="flex flex-col gap-3 p-5">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-center justify-between gap-4 border-b border-[var(--row-line)] pb-3 text-[13px] last:border-b-0">
          <dt className="text-[var(--muted-foreground)]">{label}</dt>
          <dd className="truncate font-medium text-[var(--foreground)]">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * SR-17 D3/scope item 9: renders the SAME `RecordDrawerTimelinePanel` the
 * Contact record drawer uses, fed by `GET /admin/leads/{lead_id}/timeline`.
 * "Load older" (scope item 10) writes `?lead=<id>&tab=timeline&before=<cursor>`
 * to the URL -- a fresh server fetch, not client-accumulated state.
 */
function TimelineTab({
  leadId,
  lead,
  timelineResult,
  basePath,
}: {
  leadId: string;
  lead: LeadDetail;
  timelineResult: TimelineFetchResult | null;
  basePath: string;
}) {
  if (timelineResult === null) {
    return (
      <p id="lead-tabpanel-timeline" role="tabpanel" aria-labelledby="lead-tab-timeline" className="p-5 text-sm text-muted-foreground">
        Loading timeline…
      </p>
    );
  }

  const loadOlderHref =
    timelineResult.status === "ok" && timelineResult.data.nextBefore
      ? `${basePath}?lead=${encodeURIComponent(leadId)}&tab=timeline&before=${encodeURIComponent(
          timelineResult.data.nextBefore
        )}`
      : null;

  return (
    <div id="lead-tabpanel-timeline" role="tabpanel" aria-labelledby="lead-tab-timeline" className="px-[22px] py-[18px]">
      <div className="mb-5 flex gap-2.5 rounded-[10px] bg-[var(--callout-bg)] px-3.5 py-3 text-[12.5px] leading-[1.5] text-[var(--ink-2)]">
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
        <div>{buildSummary(lead)}</div>
      </div>
      <RecordDrawerTimelinePanel result={timelineResult} loadOlderHref={loadOlderHref} />
    </div>
  );
}

function NotesTab({
  activitiesResult,
  leadId,
  tenantId,
  basePath,
}: {
  activitiesResult: LeadActivitiesResult | null;
  leadId: string;
  tenantId?: string;
  basePath: string;
}) {
  const boundAction = addLeadNote.bind(null, tenantId, leadId, basePath);
  const [state, formAction, pending] = useActionState(boundAction, initialNoteState);
  const notes =
    activitiesResult?.status === "ok"
      ? activitiesResult.items.filter((activity) => activity.type === "note")
      : [];

  return (
    <div id="lead-tabpanel-notes" role="tabpanel" aria-labelledby="lead-tab-notes" className="flex flex-1 flex-col gap-4 p-5">
      <form action={formAction} className="flex flex-col gap-2">
        <label htmlFor="note-text" className="text-xs font-semibold text-[var(--ink-2)]">
          Add a note
        </label>
        <textarea
          id="note-text"
          name="text"
          rows={3}
          maxLength={4000}
          className="rounded-lg border border-[var(--border)] p-2.5 text-[13px] text-[var(--foreground)] outline-none focus-visible:border-[var(--foreground)]"
          placeholder="Write a note about this lead…"
        />
        {state.status === "error" ? (
          <p role="alert" className="text-xs text-[var(--danger-fg)]">
            {state.message}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={pending}
          className="min-h-11 self-start rounded-lg bg-[var(--foreground)] px-4 text-[12.5px] font-bold text-white transition-opacity disabled:opacity-50"
        >
          {pending ? "Saving…" : "Save note"}
        </button>
      </form>

      {activitiesResult?.status === "error" ? (
        <p role="alert" className="rounded-lg border border-[#f6e3df] bg-[#fdf5f3] p-3 text-sm text-[var(--danger-fg)]">
          {activitiesResult.message}
        </p>
      ) : notes.length === 0 ? (
        <p className="text-sm text-muted-foreground">No notes yet.</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {notes.map((note) => (
            <li key={note.activityId} className="flex flex-col gap-1 border-b border-[var(--row-line)] pb-3 text-[12.5px] last:border-b-0">
              <span className="text-[var(--foreground)]">{typeof note.payload?.text === "string" ? note.payload.text : ""}</span>
              <span className="text-[11px] text-muted-foreground">
                {formatDateTime(note.createdAt)}
                {note.actor ? ` · ${note.actor}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
