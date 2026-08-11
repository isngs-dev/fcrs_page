/**
 * Status filter pills for the 4a conversation list (HANDOFF-SPEC.md §3:
 * "filter pills"). The mock (`id="4a"`) shows four pills -- All / Live /
 * Lead captured / Ended -- but the real backend `status` query param only
 * accepts `active`/`ended` (admin_routes.py `_VALID_STATUSES`) and there is
 * no lead-linkage field to filter on. Per the mandated scope decision, the
 * pill *values* are the real ones (All / Active / Ended) -- a fourth "Lead
 * captured" pill is NOT built (no lead-linkage field exists to back it).
 *
 * SR-27 slice 2: restyled to consume the shared `SegmentedControl`
 * (`Console.dc.html:46-48` `.seg` recipe), replacing the old hand-rolled
 * `rounded-full` pill style. Still plain `<Link>`s under the hood (via
 * `SegmentedControl`) -- server-renderable/no-JS-navigable, no "use client"
 * needed here.
 */
import { SegmentedControl } from "@/components/admin/segmented-control";
import type { ConversationStatus } from "@/lib/conversations";

const STATUS_LABELS: Record<ConversationStatus, string> = {
  active: "Active",
  ended: "Ended",
};

function pillHref(basePath: string, status: ConversationStatus | undefined): string {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  const qs = params.toString();
  return qs ? `${basePath}?${qs}` : basePath;
}

export function ConversationsFilter({
  currentStatus,
  basePath,
  statuses,
}: {
  currentStatus: ConversationStatus | undefined;
  basePath: string;
  statuses: readonly ConversationStatus[];
}) {
  return (
    <SegmentedControl
      ariaLabel="Filter conversations by status"
      items={[
        {
          key: "all",
          label: "All",
          href: pillHref(basePath, undefined),
          active: currentStatus === undefined,
        },
        ...statuses.map((status) => ({
          key: status,
          label: STATUS_LABELS[status],
          href: pillHref(basePath, status),
          active: currentStatus === status,
        })),
      ]}
    />
  );
}
