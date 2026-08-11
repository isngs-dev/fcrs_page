/**
 * Client-safe stage-rule mirror for BOTH forward-one-step-only funnels
 * (SR-18 D8). Pure data + pure functions only -- no `server-only` import, so
 * `components/admin/pipeline-board.tsx` (a Client Component, D7) can import
 * it directly.
 *
 * This is a MIRROR, not a boundary (D2/D8): every value here is copied from
 * the server's actual pipeline module and asserted, in
 * `lib/__tests__/pipelines.test.ts`, to match those values exactly, so a
 * future backend stage change fails a test here instead of silently
 * producing an unreachable column. The server re-validates every transition
 * via `validate_transition` and remains the sole enforcement point -- this
 * module never authorizes anything, it only decides which columns light up
 * as drop targets while a card is being dragged (D2).
 *
 * Leads funnel mirrors:
 *   services/api/src/api/leads/pipeline.py:38  (STAGE_ORDER)
 *   services/api/src/api/leads/pipeline.py:41  (TERMINAL_STAGES)
 *   services/api/src/api/leads/pipeline.py:71-98 (validate_transition)
 *
 * Opportunities funnel mirrors:
 *   services/api/src/api/opportunities/pipeline.py:40 (STAGE_ORDER)
 *   services/api/src/api/opportunities/pipeline.py:43 (TERMINAL_STAGES)
 *   services/api/src/api/opportunities/pipeline.py:53-75 (validate_transition)
 *
 * SR-18 follow-up (leads `converted` role gate): dropping a lead on
 * `converted` now calls the REAL `POST /admin/leads/{lead_id}/convert`
 * endpoint (creates a Contact), not the bare `PATCH .../{lead_id}` stage
 * transition -- and that endpoint is `CLIENT_ADMIN`-only
 * (services/api/src/api/leads/admin_routes.py:910, `require_roles(Role.
 * CLIENT_ADMIN)`, unlike the PATCH endpoint's CLIENT_ADMIN+CLIENT_AGENT).
 * The user explicitly chose NOT to widen the backend RBAC, so this frontend
 * mirror instead gates its own affordance: `restrictedTargets` names, per
 * pipeline, which target stages require a specific role beyond ordinary
 * forward-step legality. `legalTargets`/`isLegalTarget` take an optional
 * `currentRole` and drop any target whose required role the caller doesn't
 * hold. Omitting `currentRole` (or a pipeline with no `restrictedTargets`,
 * e.g. `OPPORTUNITIES_PIPELINE`) preserves the exact prior behavior --
 * every OTHER transition on either funnel is completely unaffected.
 */

/** A minimal role mirror -- NOT imported from `lib/auth.ts`, which is
 * `server-only` (JWT verification) and cannot be pulled into this
 * client-safe module. Values match `common.auth.Role` on the backend. */
export type PipelineRole = "PLATFORM_ADMIN" | "CLIENT_ADMIN" | "CLIENT_AGENT" | "VISITOR";

export interface PipelineDefinition {
  /** The forward funnel, in order. Mirrors the server's STAGE_ORDER. */
  stageOrder: readonly string[];
  /** Stages from which no further transition is permitted (D5 no-reopen). */
  terminalStages: ReadonlySet<string>;
  /** The one off-ramp stage, reachable from any non-terminal stage. */
  offRamp: string;
  /** Target stages that require the caller to hold one of a specific set of
   * roles, ON TOP OF the ordinary forward-step/off-ramp legality check --
   * never a replacement for it. Absent or empty for a stage means no extra
   * gate (the pipeline-wide default). Only `LEADS_PIPELINE.converted` sets
   * this today, mirroring `POST /admin/leads/{lead_id}/convert`'s
   * `CLIENT_ADMIN`-only RBAC. */
  restrictedTargets?: Readonly<Record<string, readonly PipelineRole[]>>;
}

/** Mirrors leads/pipeline.py:38,41 exactly. */
export const LEADS_PIPELINE: PipelineDefinition = {
  stageOrder: ["captured", "qualified", "contacted", "converted"],
  terminalStages: new Set(["converted", "disqualified"]),
  offRamp: "disqualified",
  restrictedTargets: {
    // The final step into `converted` goes through the real convert
    // endpoint (creates a Contact), which is CLIENT_ADMIN-only. Every
    // other leads transition (captured->qualified->contacted, and the
    // disqualified off-ramp from any non-terminal stage) is unrestricted.
    converted: ["CLIENT_ADMIN"],
  },
};

/** Mirrors opportunities/pipeline.py:40,43 exactly. */
export const OPPORTUNITIES_PIPELINE: PipelineDefinition = {
  stageOrder: ["prospecting", "qualification", "proposal", "negotiation", "closed_won"],
  terminalStages: new Set(["closed_won", "closed_lost"]),
  offRamp: "closed_lost",
};

/**
 * The set of legal drop targets for a card currently at `current` (D2/D8).
 * Exactly the immediate next stage (if any, and if `current` is not
 * terminal) plus the off-ramp (unless `current` IS the off-ramp or already
 * terminal) -- nothing else, ever. Mirrors `validate_transition`'s logic
 * (both `leads/pipeline.py` and `opportunities/pipeline.py` share this exact
 * shape) but only to decide affordances, never to authorize a write.
 *
 * A terminal `current` returns an empty set -- the caller (`isDraggable`)
 * uses this to refuse the drag gesture entirely, not merely to show no
 * targets mid-drag.
 *
 * `currentRole` (optional): when a candidate target has an entry in
 * `pipeline.restrictedTargets`, it is only included if `currentRole` is one
 * of that entry's roles. Omitting `currentRole` excludes every restricted
 * target (fail-closed, matching a caller that hasn't identified itself);
 * pipelines with no `restrictedTargets` (e.g. `OPPORTUNITIES_PIPELINE`) are
 * completely unaffected by this parameter either way.
 */
export function legalTargets(
  pipeline: PipelineDefinition,
  current: string,
  currentRole?: PipelineRole
): string[] {
  if (pipeline.terminalStages.has(current)) {
    return [];
  }

  const targets: string[] = [];
  const currentIndex = pipeline.stageOrder.indexOf(current);
  if (currentIndex !== -1 && currentIndex + 1 < pipeline.stageOrder.length) {
    targets.push(pipeline.stageOrder[currentIndex + 1]);
  }

  // The off-ramp is available from any non-terminal stage, including one
  // not found in stageOrder at all defensively -- but in practice `current`
  // is always either in stageOrder or in terminalStages for both funnels.
  targets.push(pipeline.offRamp);

  return targets.filter((target) => {
    const requiredRoles = pipeline.restrictedTargets?.[target];
    if (!requiredRoles || requiredRoles.length === 0) return true;
    return currentRole !== undefined && requiredRoles.includes(currentRole);
  });
}

/** A card in a terminal stage is not draggable at all (D2/D5 no-reopen). */
export function isDraggable(pipeline: PipelineDefinition, current: string): boolean {
  return !pipeline.terminalStages.has(current);
}

/** Whether `target` is a legal drop target for a card currently at
 * `current`. Convenience wrapper over `legalTargets` for the board's
 * per-column inertness check. `currentRole` is forwarded to `legalTargets`
 * unchanged -- see its doc comment for the role-gating rules. */
export function isLegalTarget(
  pipeline: PipelineDefinition,
  current: string,
  target: string,
  currentRole?: PipelineRole
): boolean {
  return legalTargets(pipeline, current, currentRole).includes(target);
}

/** Whether `stage` is the pipeline's off-ramp -- used to decide whether a
 * drop must go through the confirmation dialog (D4) instead of submitting
 * directly. */
export function isOffRamp(pipeline: PipelineDefinition, stage: string): boolean {
  return stage === pipeline.offRamp;
}
