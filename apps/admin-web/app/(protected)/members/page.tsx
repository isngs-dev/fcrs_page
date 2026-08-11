/**
 * Team members screen (7b), CLIENT_ADMIN-only -- gated by `requireRole`
 * (matches `tenants/new/page.tsx`'s PLATFORM_ADMIN gate). Restyled to 7b's
 * visual language (HANDOFF-SPEC.md §1/§2/§3 "7b"), wired to the three real
 * `/admin/users` endpoints only.
 *
 * 7b elements deliberately omitted or stubbed (locked scope decision):
 *  - Pending-invite banner: OMITTED. There is no invite/token/pending-status
 *    concept anywhere in `users_routes.py` -- `POST /admin/users` creates a
 *    user immediately. Rendering an always-empty invite banner would be
 *    dead UI, so it is dropped rather than faked.
 *  - Open-lead-load column: OMITTED. Confirmed by reading
 *    `services/api/src/api/leads/admin_routes.py` in full -- leads carry an
 *    `assigned_agent_id` but there is no per-agent aggregation/count
 *    endpoint. Fabricating a number here would violate "no silent
 *    fallbacks" (CLAUDE.md §3).
 *  - Auto-assignment toggle: SR-20 D1/D2 replaces the stub with the real
 *    round-robin toggle, bound to `GET`/`PUT /admin/assignment-config`.
 *    CLIENT_ADMIN-only (this page already gates on CLIENT_ADMIN, so a
 *    CLIENT_AGENT never reaches this component at all).
 */
import { requireRole } from "@/lib/auth";
import { listMembers } from "@/lib/members";
import { getAssignmentConfig } from "@/lib/assignment";
import { MembersTable } from "@/app/(protected)/members/members-table";
import { CreateMemberDialog } from "@/app/(protected)/members/create-member-dialog";
import { AssignmentToggle } from "@/app/(protected)/members/assignment-toggle";

export default async function MembersPage() {
  await requireRole("CLIENT_ADMIN");

  const result = await listMembers();
  const assignmentResult = await getAssignmentConfig();

  return (
    <div className="flex flex-1 flex-col gap-[18px] p-[22px] md:p-[28px]">
      <div className="flex items-start justify-between gap-[14px]">
        <div>
          <h1 className="text-[28px] font-semibold text-foreground">Team members</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {result.status === "ok"
              ? `${result.items.length} member${result.items.length === 1 ? "" : "s"}`
              : "Team members"}
          </p>
        </div>
        <CreateMemberDialog />
      </div>

      {result.status === "error" ? (
        <div
          role="alert"
          className="rounded-xl border border-[#f0e2bd] bg-[#fff9ec] px-4 py-3 text-[13px] text-[#6a4e00]"
        >
          {result.message}
          {result.correlationId ? (
            <span className="block text-[11px] text-[#6a4e00]/80">
              Correlation ID: {result.correlationId}
            </span>
          ) : null}
        </div>
      ) : (
        <MembersTable members={result.items} />
      )}

      {/* SR-20 D1/D2: the real round-robin toggle. */}
      {assignmentResult.status === "ok" ? (
        <AssignmentToggle initialEnabled={assignmentResult.config.roundRobinEnabled} />
      ) : (
        <div
          role="alert"
          className="rounded-xl border border-[#f0e2bd] bg-[#fff9ec] px-4 py-3 text-[13px] text-[#6a4e00]"
        >
          {assignmentResult.message}
        </div>
      )}
    </div>
  );
}
