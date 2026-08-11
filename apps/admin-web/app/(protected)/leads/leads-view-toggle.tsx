/**
 * 4b Board/Table segmented toggle (HANDOFF-SPEC.md §4: "Board/Table is a
 * segmented toggle, state in URL"). `?view=board` now renders the real
 * `PipelineBoard` kanban (SR-18 M6) -- real drag-and-drop writing
 * `PATCH /admin/leads/{id}` against live lead data, constrained to the
 * transitions the backend's forward-one-step-only funnel actually permits
 * (SR-18 D2). SR-15 shipped this toggle wired to `?view=` with an honest
 * "coming soon" panel standing in for the board, on the stated grounds that
 * a kanban against production lead data needed its own careful pass; SR-18
 * is that pass, and the placeholder panel it left in `leads/page.tsx` is
 * retired by this sprint (see that file's header comment).
 *
 * SR-24: restyled to the reference's `.seg` recipe (Console.dc.html:46-48) --
 * cream track, 3px padding, 10px-radius pill buttons, dark-active state.
 * Render order corrected to Table-first (was Board-first) to match the
 * reference's artboard order; the `hrefFor`/URL semantics are unchanged.
 *
 * SR-27 slice 0: now a thin wrapper over the shared
 * `components/admin/segmented-control.tsx` primitive -- this file used to
 * hand-roll the `.seg` CSS; that recipe is now shared with Conversations
 * (and any future consumer) instead of living in two copies.
 */
import { SegmentedControl } from "@/components/admin/segmented-control";

export function LeadsViewToggle({
  view,
  basePath,
  currentParams,
}: {
  view: "table" | "board";
  basePath: string;
  currentParams: URLSearchParams;
}) {
  function hrefFor(nextView: "table" | "board"): string {
    const params = new URLSearchParams(currentParams);
    params.delete("lead");
    params.delete("tab");
    if (nextView === "table") {
      params.delete("view");
    } else {
      params.set("view", nextView);
    }
    const qs = params.toString();
    return qs ? `${basePath}?${qs}` : basePath;
  }

  return (
    <SegmentedControl
      ariaLabel="Leads view"
      items={[
        { key: "table", label: "Table", href: hrefFor("table"), active: view === "table" },
        { key: "board", label: "Board", href: hrefFor("board"), active: view === "board" },
      ]}
    />
  );
}
