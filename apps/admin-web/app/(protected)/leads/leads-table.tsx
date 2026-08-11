/**
 * 4b leads table (HANDOFF-SPEC.md §2 Tables + §2 Badges). Restyled from the
 * original shadcn `<Table>` to the exact design recipe: header row var(--secondary)
 * 11.5px/600 uppercase muted, rows 13px with `border-faint` dividers, stage
 * badges + score chip per the color table, initials avatar for the assigned
 * agent. Still a pure presentation component fed rows the server component
 * already fetched -- columns are the same leak-free `LeadListItem` fields,
 * just restyled (Name, Email, Stage, Status, Score, Assigned, Created).
 *
 * Each row is a link to `{basePath}?...&lead={id}` (opens the 4b drawer,
 * HANDOFF-SPEC.md §4 "Drawer opens from table row or 'View lead'") --
 * `<Link>`, not a client onClick, so it's keyboard/middle-click/right-click
 * navigable like any other link in this server-first codebase, and works
 * with JS disabled (progressive enhancement).
 *
 * SR-25 replaces the former decorative header state with real, server-only
 * sort Links and <details> filter funnels. Name/Email share the toolbar's
 * combined q search (no redundant per-column funnel); score is sortable but
 * intentionally unfilterable. Every affordance preserves URL state.
 */
import Link from "next/link";
import type { ReactNode } from "react";
import type { Role } from "@/lib/auth";
import type { LeadListItem, LeadSortDirection, LeadSortKey } from "@/lib/leads";
import { initialsFromName, scoreChipStyle, stageBadgeStyle } from "@/lib/leads";
import { TableCard, TableHeadCell, TableCell, TableRow } from "@/components/admin/table-card";
import { Chip } from "@/components/admin/chip";
import { ColumnFilterMenu, type ColumnFilterOption } from "@/components/admin/column-filter-menu";
import { ColumnSortLink } from "@/components/admin/column-sort-link";
import { defaultLeadSortDirection } from "@/lib/leads";

const MUTED = <span className="text-muted-foreground">—</span>;

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function leadHref(basePath: string, currentParams: URLSearchParams, leadId: string): string {
  const params = new URLSearchParams(currentParams);
  params.set("lead", leadId);
  params.delete("tab");
  return `${basePath}?${params.toString()}`;
}

type FilterKey = "stage" | "status" | "assigned" | "created";

interface HeaderDescriptor {
  key: string;
  label: string;
  sortKey: LeadSortKey;
  filter?: FilterKey;
}

interface LeadAgentOption {
  id: string;
  label: string;
}

const HEADERS: HeaderDescriptor[] = [
  { key: "name", label: "Name", sortKey: "name" },
  { key: "email", label: "Email", sortKey: "email" },
  { key: "stage", label: "Stage", sortKey: "stage", filter: "stage" },
  { key: "status", label: "Status", sortKey: "status", filter: "status" },
  { key: "score", label: "Score", sortKey: "score" },
  { key: "assigned", label: "Assigned", sortKey: "assigned", filter: "assigned" },
  { key: "created", label: "Created", sortKey: "created", filter: "created" },
];

// Decorative, `aria-hidden` leading icons for the header labels only
// (person/envelope/flag/star) -- purely presentational, no sort/filter
// affordance implied.
const ICON_PROPS = {
  width: 12,
  height: 12,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const HEADER_ICONS: Record<string, ReactNode> = {
  Name: (
    <svg {...ICON_PROPS}>
      <circle cx="12" cy="8" r="3.2" />
      <path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6" />
    </svg>
  ),
  Email: (
    <svg {...ICON_PROPS}>
      <rect x="3.5" y="5.5" width="17" height="13" rx="2" />
      <path d="M4 7l8 6 8-6" />
    </svg>
  ),
  Stage: (
    <svg {...ICON_PROPS}>
      <path d="M5 3v18" />
      <path d="M5 4h11l-2.5 3.5L16 11H5" />
    </svg>
  ),
  Score: (
    <svg {...ICON_PROPS}>
      <path d="M12 3l2.6 5.7 6.2.6-4.7 4.1 1.4 6.1L12 16.9l-5.5 2.6 1.4-6.1-4.7-4.1 6.2-.6z" />
    </svg>
  ),
};

// Roughly matches the reference's `150px 1.5fr 140px 90px 80px 1.5fr 90px`
// grid template (without the checkbox column we deliberately don't build),
// adapted to `<colgroup>` proportions for a real `<table>` layout.
const COLUMN_WIDTHS = ["15%", "22%", "14%", "10%", "9%", "18%", "12%"];

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function filterOptions(
  filter: FilterKey,
  currentParams: URLSearchParams,
  currentRole: Role | undefined,
  currentUserId: string | undefined,
  agents: LeadAgentOption[] | undefined
): ColumnFilterOption[] {
  if (filter === "stage") {
    const current = currentParams.get("stage");
    return [
      { label: "All stages", values: { stage: undefined }, active: !current },
      ...["captured", "qualified", "contacted", "converted", "disqualified"].map((stage) => ({
        label: titleCase(stage), values: { stage }, active: current === stage,
      })),
    ];
  }
  if (filter === "status") {
    const current = currentParams.get("status");
    return [
      { label: "All statuses", values: { status: undefined }, active: !current },
      ...["new", "open", "won", "lost"].map((status) => ({
        label: titleCase(status), values: { status }, active: current === status,
      })),
    ];
  }
  if (filter === "assigned") {
    const current = currentParams.get("assigned_agent_id");
    const options: ColumnFilterOption[] = [
      { label: "All", values: { assigned_agent_id: undefined }, active: !current },
    ];
    // Assignment only permits active CLIENT_AGENT targets, so a CLIENT_ADMIN
    // would get a dead empty "Assigned to me" filter. Keep that option to
    // the role for which it is real; admins receive the active-agent list.
    if (currentRole === "CLIENT_AGENT" && currentUserId) {
      options.push({
        label: "Assigned to me",
        values: { assigned_agent_id: currentUserId },
        active: current === currentUserId,
      });
    }
    if (currentRole === "CLIENT_ADMIN") {
      for (const agent of agents ?? []) {
        options.push({
          label: agent.label,
          values: { assigned_agent_id: agent.id },
          active: current === agent.id,
        });
      }
    }
    return options;
  }

  const now = new Date();
  const range = (days: number) => new Date(now.getTime() - days * 86_400_000).toISOString();
  const hasDateFilter = Boolean(currentParams.get("from") || currentParams.get("to"));
  return [
    { label: "All time", values: { from: undefined, to: undefined }, active: !hasDateFilter },
    { label: "Last 7 days", values: { from: range(7), to: now.toISOString() } },
    { label: "Last 30 days", values: { from: range(30), to: now.toISOString() } },
    { label: "Last 90 days", values: { from: range(90), to: now.toISOString() } },
  ];
}

/** SR-24 item 13: stage cell is always the neutral cream `Chip` with a
 * colored dot carrying the per-stage color, PLUS the label text (the text
 * is always present -- color is reinforcement, never the only signal). */
function stageDotColor(stage: string): string {
  switch (stage) {
    case "converted":
      return "var(--success-dot)";
    case "disqualified":
      return "#a24b4b";
    case "qualified":
      return "#333333";
    case "contacted":
      return "#404040";
    default:
      return "#878787";
  }
}

export function LeadsTable({
  items,
  basePath,
  currentParams,
  selectedLeadId,
  sort,
  direction,
  currentRole,
  currentUserId,
  agents,
  agentsUnavailable,
  footer,
}: {
  items: LeadListItem[];
  /** Base list route (`/leads` or `/clients/{tenantId}/leads`) used to build
   * each row's `?lead=` link -- mirrors `leads-filter.tsx`'s `basePath`. */
  basePath?: string;
  /** Existing query params (stage/page/view) to preserve when adding `lead=`
   * to a row's href, so opening the drawer doesn't drop the current filter. */
  currentParams?: URLSearchParams;
  /** The currently open lead (from `?lead=`), highlighted per HANDOFF-SPEC.md
   * §2 "highlighted row #fdfdec". */
  selectedLeadId?: string;
  sort?: string;
  direction?: LeadSortDirection;
  currentRole?: Role;
  currentUserId?: string;
  agents?: LeadAgentOption[];
  agentsUnavailable?: boolean;
  /** SR-24 item 17: `LeadsPagination`, rendered inside the same bordered
   * `TableCard` as a footer row instead of a separate component below it. */
  footer?: ReactNode;
}) {
  const resolvedBasePath = basePath ?? "/leads";
  const resolvedParams = currentParams ?? new URLSearchParams();

  return (
    <TableCard footer={footer} allowOverflow>
      <colgroup>
        {COLUMN_WIDTHS.map((width, index) => (
          <col key={HEADERS[index].key} style={{ width }} />
        ))}
      </colgroup>
      <thead>
        <tr>
          {HEADERS.map((header) => (
            <TableHeadCell
              key={header.key}
              leftIcon={HEADER_ICONS[header.label]}
              aria-sort={
                sort === header.sortKey
                  ? direction === "asc"
                    ? "ascending"
                    : "descending"
                  : "none"
              }
              rightControls={
                <>
                  <ColumnSortLink
                    label={header.label}
                    sortKey={header.sortKey}
                    basePath={resolvedBasePath}
                    currentParams={resolvedParams}
                    currentSort={sort}
                    currentDirection={direction}
                    defaultDirection={defaultLeadSortDirection(header.sortKey)}
                    dropParams={["lead", "tab"]}
                  />
                  {header.filter ? (
                    <ColumnFilterMenu
                      label={header.label}
                      basePath={resolvedBasePath}
                      currentParams={resolvedParams}
                      dropParams={["lead", "tab"]}
                      options={filterOptions(
                        header.filter,
                        resolvedParams,
                        currentRole,
                        currentUserId,
                        agents
                      )}
                      unavailableMessage={
                        header.filter === "assigned" && currentRole === "CLIENT_ADMIN" && agentsUnavailable
                          ? "Agent list is unavailable."
                          : undefined
                      }
                    />
                  ) : null}
                </>
              }
            >
              {header.label}
            </TableHeadCell>
          ))}
        </tr>
      </thead>
      <tbody>
        {items.map((lead) => {
          const stageBadge = stageBadgeStyle(lead.stage);
          const scoreBadge =
            lead.qualificationScore !== null ? scoreChipStyle(lead.qualificationScore, lead.stage) : null;
          const highlighted = lead.leadId === selectedLeadId;
          return (
            <TableRow
              key={lead.leadId}
              style={highlighted ? { background: "#fdfdec" } : undefined}
            >
              <TableCell className="px-0 py-0">
                <Link
                  href={leadHref(resolvedBasePath, resolvedParams, lead.leadId)}
                  scroll={false}
                  className="block min-h-11 px-3.5 py-3 font-bold text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                >
                  {lead.name ?? MUTED}
                </Link>
              </TableCell>
              <TableCell className="text-[var(--ink-2)]">{lead.email ?? MUTED}</TableCell>
              <TableCell>
                {/* SR-24 item 13: always the neutral cream Chip -- the
                    per-stage color moves onto the decorative dot, the label
                    text is always present (color is reinforcement, never the
                    only signal, per D4). */}
                <Chip className="h-[22px]">
                  <span
                    aria-hidden
                    className="size-1.5 shrink-0 rounded-full"
                    style={{ background: stageDotColor(lead.stage) }}
                  />
                  {stageBadge.label}
                </Chip>
              </TableCell>
              <TableCell className="text-[var(--ink-2)] capitalize">{lead.status}</TableCell>
              <TableCell>
                {scoreBadge ? (
                  lead.stage === "converted" ? (
                    <Chip tone="success" dot>
                      {scoreBadge.label}
                    </Chip>
                  ) : (
                    <span
                      className="rounded-md px-1.5 py-0.5 font-bold"
                      style={{ background: scoreBadge.bg, color: scoreBadge.fg }}
                    >
                      {scoreBadge.label}
                    </span>
                  )
                ) : (
                  MUTED
                )}
              </TableCell>
              <TableCell>
                {lead.assignedAgentId ? (
                  <span className="flex items-center gap-1.5 text-[var(--ink-2)]">
                    <span className="grid size-[26px] shrink-0 place-items-center rounded-full bg-secondary text-[9px] font-bold text-[var(--ink-2)]">
                      {initialsFromName(lead.assignedAgentId)}
                    </span>
                    {lead.assignedAgentId}
                  </span>
                ) : (
                  <span className="text-muted-foreground">— Assign</span>
                )}
              </TableCell>
              <TableCell className="text-muted-foreground">{formatDate(lead.createdAt)}</TableCell>
            </TableRow>
          );
        })}
      </tbody>
    </TableCard>
  );
}
