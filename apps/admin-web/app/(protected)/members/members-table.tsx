"use client";

/**
 * Members table (7b), restyled to Console.dc.html:333-358's exact geometry:
 * `.tbl-card`/`.th-cell`/`.td` recipe via `TableCard`/`TableHeadCell`/
 * `TableCell`/`TableRow`, icon-leading column labels, 32px avatar, role/status
 * `.chip`s, `.btn-outline.btn-sm` Deactivate/Activate button. Columns are
 * MEMBER / ROLE / LAST ACTIVE / STATUS / actions -- the checkbox column and
 * the filter-funnel icons from the reference are DELIBERATELY NOT built (see
 * the sort/filter/checkbox reasoning below); open-lead-load column remains
 * omitted (see `page.tsx` header comment).
 *
 * Sort: unlike the Leads table's `ColumnSortLink` (SR-25), which mutates the
 * URL to ask the SERVER to re-sort a paginated fetch, this table already has
 * every row of a tenant's team in memory (`useState(members)`, no
 * pagination) -- so a click-to-sort control here re-orders real, complete
 * data instead of pointing at a non-existent backend `sort` param or
 * silently lying about rows the client hasn't fetched. That makes a
 * client-side sort control an honest affordance for this table specifically,
 * NOT a dead control, even though the identical-looking icon would be dead
 * on a paginated table. Implemented as a local click handler + local sort
 * state, deliberately NOT importing `ColumnSortLink` (Link/URL-based,
 * assumes server-side pagination -- wrong mechanism here). Available on
 * Member (name), Role, and Last active -- Status is skipped (binary
 * Active/Inactive, sorting two values is low value). No filter icons and no
 * leading checkbox column: there is no backend filter param on
 * `GET /admin/users` and no bulk-mutation endpoint (only single-user
 * `PATCH /admin/users/{user_id}`), so both would be decorative-looking dead
 * controls (CLAUDE.md's no-dead-controls principle).
 *
 * The active/inactive toggle requires confirmation before deactivating
 * (accessibility instructions: a destructive-ish action affecting someone's
 * access). Reactivating (a non-destructive action) does not require
 * confirmation.
 */
import { useMemo, useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { TableCard, TableHeadCell, TableCell, TableRow } from "@/components/admin/table-card";
import { Chip } from "@/components/admin/chip";
import type { MemberSummary } from "@/lib/members";
import { formatLastActive, initialsFromMember, roleBadgeStyle } from "@/lib/members-presentation";
import { toggleMemberActiveAction } from "@/app/(protected)/members/actions";

type SortColumn = "member" | "role" | "lastActive";
type SortDirection = "asc" | "desc";
interface SortState {
  column: SortColumn;
  direction: SortDirection;
}

const ICON_PROPS = {
  width: 13,
  height: 13,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const MEMBER_ICON = (
  <svg {...ICON_PROPS}>
    <circle cx="12" cy="8" r="4" />
    <path d="M4 20a8 8 0 0 1 16 0" />
  </svg>
);

const ROLE_ICON = (
  <svg {...ICON_PROPS}>
    <path d="M12 2 4 6v6c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6z" />
  </svg>
);

const LAST_ACTIVE_ICON = (
  <svg {...ICON_PROPS}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

function sortValue(member: MemberSummary, column: SortColumn): string | number {
  if (column === "member") return (member.name ?? member.email).toLowerCase();
  if (column === "role") return roleBadgeStyle(member.role).label;
  // "Never logged in" sorts as epoch 0 (oldest) rather than fabricating a
  // fake recent timestamp.
  return member.lastLoginAt ? new Date(member.lastLoginAt).getTime() : 0;
}

function sortRows(rows: MemberSummary[], sort: SortState | null): MemberSummary[] {
  if (!sort) return rows;
  const { column, direction } = sort;
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = sortValue(a, column);
    const bv = sortValue(b, column);
    if (av < bv) return -1 * sign;
    if (av > bv) return 1 * sign;
    return 0;
  });
}

/** Local, purely client-side sort control -- re-orders the already-fetched
 * `rows` array in place. Deliberately not `ColumnSortLink` (that component
 * is Link/URL-driven for a server-paginated fetch; this table has neither). */
function SortButton({
  column,
  label,
  sort,
  onSort,
}: {
  column: SortColumn;
  label: string;
  sort: SortState | null;
  onSort: (column: SortColumn) => void;
}) {
  const active = sort?.column === column;
  const glyph = active && sort?.direction === "asc" ? "↑" : active ? "↓" : "↕";
  const nextAction = !active
    ? `Sort ${label} ascending`
    : sort?.direction === "asc"
      ? `Sort ${label} descending`
      : `Clear ${label} sort`;

  return (
    <button
      type="button"
      aria-label={nextAction}
      onClick={() => onSort(column)}
      className="grid size-5 place-items-center rounded text-[13px] leading-none text-muted-foreground hover:bg-[#e6e6e6] hover:text-[var(--ink-2)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring"
    >
      <span aria-hidden>{glyph}</span>
    </button>
  );
}

function RoleChip({ role }: { role: string }) {
  // `Chip` doesn't expose a style override (its `tone` prop is a fixed
  // vocabulary, not an arbitrary-color escape hatch) -- the role badge needs
  // a per-role background (admin dark / agent cream), which is genuinely
  // outside that vocabulary, so this keeps the same inline-styled-span
  // approach the table used pre-restyle, just matched to the reference's
  // `.chip` geometry (24px tall, 10px horizontal padding, 7px radius,
  // 11.5px/600 text -- Console.dc.html:37) instead of a bespoke recipe.
  const style = roleBadgeStyle(role);
  return (
    <span
      className="inline-flex h-6 items-center rounded-[7px] px-2.5 text-[11.5px] font-semibold"
      style={{ background: style.bg, color: style.fg }}
    >
      {style.label}
    </span>
  );
}

function Avatar({ member }: { member: MemberSummary }) {
  const initials = initialsFromMember(member.name, member.email);
  const isAdmin = member.role === "CLIENT_ADMIN";
  return (
    <span
      className="flex h-8 w-8 flex-none items-center justify-center rounded-full text-[11px] font-bold"
      style={
        isAdmin
          ? { background: "#333333", color: "#ffffff" }
          : { background: "#cdcdcd", color: "var(--ink-2)" }
      }
      aria-hidden="true"
    >
      {initials}
    </span>
  );
}

function DeactivateConfirmDialog({
  member,
  onConfirm,
  onCancel,
  pending,
}: {
  member: MemberSummary;
  onConfirm: () => void;
  onCancel: () => void;
  pending: boolean;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="deactivate-dialog-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    >
      <div className="flex w-full max-w-sm flex-col gap-4 rounded-2xl border border-border bg-card p-5 shadow-xl">
        <div>
          <h2 id="deactivate-dialog-title" className="text-[15px] font-bold text-foreground">
            Deactivate {member.name ?? member.email}?
          </h2>
          <p className="mt-1.5 text-[13px] text-[var(--ink-2)]">
            They will immediately lose access to this tenant&apos;s console. You can reactivate
            them at any time.
          </p>
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" onClick={onConfirm} disabled={pending}>
            {pending ? "Deactivating…" : "Deactivate"}
          </Button>
        </div>
      </div>
    </div>
  );
}

// Reference grid-template-columns: 44px 2fr 150px 170px 130px 130px (a
// leading checkbox column, Member, Role, Last active, Status, actions). The
// checkbox column is dropped (no bulk endpoint -- see file header); the
// remaining five columns keep the reference's relative proportions via
// `<colgroup>`, matching the pattern `leads-table.tsx` already established
// for adapting a CSS-grid artboard onto a real `<table>`.
const COLUMN_WIDTHS = ["40%", "16%", "18%", "13%", "13%"];

export function MembersTable({ members }: { members: MemberSummary[] }) {
  const [rows, setRows] = useState(members);
  const [sort, setSort] = useState<SortState | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<MemberSummary | null>(null);
  const [errorByMember, setErrorByMember] = useState<Record<string, string>>({});
  const [isPending, startTransition] = useTransition();

  const sortedRows = useMemo(() => sortRows(rows, sort), [rows, sort]);

  function handleSort(column: SortColumn) {
    setSort((prev) => {
      if (!prev || prev.column !== column) return { column, direction: "asc" };
      if (prev.direction === "asc") return { column, direction: "desc" };
      return null;
    });
  }

  function applyToggle(userId: string, active: boolean) {
    setErrorByMember((prev) => {
      const next = { ...prev };
      delete next[userId];
      return next;
    });
    startTransition(async () => {
      const result = await toggleMemberActiveAction(userId, active);
      if (result.status === "ok" && result.member) {
        const updated = result.member;
        setRows((prev) => prev.map((m) => (m.id === userId ? updated : m)));
      } else {
        setErrorByMember((prev) => ({
          ...prev,
          [userId]: result.message ?? "Something went wrong.",
        }));
      }
    });
  }

  function handleToggleClick(member: MemberSummary) {
    if (member.active) {
      // Destructive-ish: confirm before deactivating.
      setConfirmTarget(member);
    } else {
      applyToggle(member.id, true);
    }
  }

  if (rows.length === 0) {
    return (
      <div className="rounded-[14px] border border-border p-8 text-center text-[13px] text-muted-foreground">
        No team members yet.
      </div>
    );
  }

  return (
    <>
      <TableCard>
        <colgroup>
          {COLUMN_WIDTHS.map((width, index) => (
            <col key={index} style={{ width }} />
          ))}
        </colgroup>
        <thead>
          <tr>
            <TableHeadCell
              className="pl-4"
              leftIcon={MEMBER_ICON}
              aria-sort={sort?.column === "member" ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}
              rightControls={<SortButton column="member" label="Member" sort={sort} onSort={handleSort} />}
            >
              Member
            </TableHeadCell>
            <TableHeadCell
              leftIcon={ROLE_ICON}
              aria-sort={sort?.column === "role" ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}
              rightControls={<SortButton column="role" label="Role" sort={sort} onSort={handleSort} />}
            >
              Role
            </TableHeadCell>
            <TableHeadCell
              leftIcon={LAST_ACTIVE_ICON}
              aria-sort={
                sort?.column === "lastActive" ? (sort.direction === "asc" ? "ascending" : "descending") : "none"
              }
              rightControls={<SortButton column="lastActive" label="Last active" sort={sort} onSort={handleSort} />}
            >
              Last active
            </TableHeadCell>
            <TableHeadCell>Status</TableHeadCell>
            <TableHeadCell className="pr-4 text-right">
              <span className="sr-only">Actions</span>
            </TableHeadCell>
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((member) => (
            <TableRow key={member.id}>
              <TableCell className="pl-4">
                <div className="flex items-center gap-3">
                  <Avatar member={member} />
                  <div className="min-w-0">
                    <div className="truncate text-[13px] font-semibold text-foreground">
                      {member.name ?? member.email}
                    </div>
                    <div className="truncate text-[12px] text-muted-foreground">{member.email}</div>
                  </div>
                </div>
              </TableCell>
              <TableCell>
                <RoleChip role={member.role} />
              </TableCell>
              <TableCell className="text-muted-foreground">
                {formatLastActive(member.lastLoginAt)}
              </TableCell>
              <TableCell>
                {/* D4: success is never color-only -- "Active"/"Inactive"
                    text carries the meaning; the dot is decorative. */}
                {member.active ? (
                  <Chip tone="success" dot>
                    Active
                  </Chip>
                ) : (
                  <span className="inline-flex h-6 items-center rounded-[7px] bg-[#f6e3df] px-2.5 text-[11.5px] font-semibold text-[var(--danger-fg)]">
                    Inactive
                  </span>
                )}
                {errorByMember[member.id] ? (
                  <p role="alert" className="mt-1 text-[11px] text-[var(--danger-fg)]">
                    {errorByMember[member.id]}
                  </p>
                ) : null}
              </TableCell>
              <TableCell className="pr-4 text-right">
                <Button
                  type="button"
                  variant="outline"
                  disabled={isPending}
                  onClick={() => handleToggleClick(member)}
                  className="h-8 rounded-[9px] border-border bg-card px-[13px] text-[12.5px] font-semibold text-[var(--ink-2)] hover:bg-[#e6e6e6]"
                >
                  {member.active ? "Deactivate" : "Activate"}
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </tbody>
      </TableCard>

      {confirmTarget ? (
        <DeactivateConfirmDialog
          member={confirmTarget}
          pending={isPending}
          onCancel={() => setConfirmTarget(null)}
          onConfirm={() => {
            applyToggle(confirmTarget.id, false);
            setConfirmTarget(null);
          }}
        />
      ) : null}
    </>
  );
}
