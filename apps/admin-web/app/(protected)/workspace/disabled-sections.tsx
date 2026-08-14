/**
 * Danger-zone disabled/coming-soon section (SR-20 D7, geometry rebuilt
 * SR-27 slice 7 -- D2). Rendered at the reference's exact card recipe
 * (`Console.dc.html:885-891`: `#e2b8b8` border, `#a24b4b` heading) -- real
 * visual fidelity -- but the Delete-workspace button renders `disabled`
 * with the SR-20 D7 explanation preserved verbatim. There is no DELETE
 * endpoint anywhere on `/admin/workspace` or `/admin/tenants/{id}` (G19) --
 * shipping a clickable button here would be a dead control for the single
 * most irreversible action in the product. Billing has NO card here at all
 * (the reference doesn't give it one either) -- it is a disabled rail entry
 * only, in `settings-rail.tsx`.
 */
import { SoftCard } from "@/components/admin/soft-card";
import { Button } from "@/components/ui/button";

export function DisabledSections() {
  return (
    <SoftCard
      className="flex scroll-mt-16 flex-col gap-3 px-[22px] pb-[18px] pt-1"
      style={{ borderColor: "#e2b8b8" }}
      id="settings-danger-zone"
    >
      <h2 className="pt-4 text-[15px] font-semibold" style={{ color: "#a24b4b" }}>
        Danger zone
      </h2>
      <div className="flex items-center justify-between gap-5">
        <div>
          <p className="text-[13px] font-semibold text-foreground">Delete workspace</p>
          <p className="mt-[3px] max-w-md text-xs leading-[1.45] text-muted-foreground">
            Permanently remove this workspace and all of its data. This cannot be undone.
            Workspace deletion isn&apos;t available yet — this is the most irreversible action
            in the product and deserves its own dedicated flow. Contact support if you need a
            workspace removed.
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          disabled
          aria-disabled="true"
          className="flex-none disabled:opacity-100"
          style={{ borderColor: "#d99", color: "#a24b4b" }}
        >
          Delete workspace
        </Button>
      </div>
    </SoftCard>
  );
}
