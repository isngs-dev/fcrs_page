/**
 * Exhaustive stage-rule correctness tests (SR-18 spec "Tests" section,
 * MANDATORY, this sprint's highest-value tests). Every stage of BOTH
 * funnels is asserted -- not spot-checked -- against the server's actual
 * `pipeline.py` values, so a future backend stage change fails a test here
 * instead of silently producing an unreachable column (D8).
 */
import { describe, expect, it } from "vitest";
import {
  LEADS_PIPELINE,
  OPPORTUNITIES_PIPELINE,
  isDraggable,
  isLegalTarget,
  isOffRamp,
  legalTargets,
} from "@/lib/pipelines";

// ---------------------------------------------------------------------------
// D8: the client mirror matches the server's actual values EXACTLY.
// These literal arrays/sets are transcribed by hand from the live files
// read during this sprint's investigation:
//   services/api/src/api/leads/pipeline.py:38,41
//   services/api/src/api/opportunities/pipeline.py:40,43
// ---------------------------------------------------------------------------
const SERVER_LEAD_STAGE_ORDER = ["captured", "qualified", "contacted", "converted"];
const SERVER_LEAD_TERMINAL_STAGES = new Set(["converted", "disqualified"]);
const SERVER_LEAD_OFF_RAMP = "disqualified";

const SERVER_OPPORTUNITY_STAGE_ORDER = [
  "prospecting",
  "qualification",
  "proposal",
  "negotiation",
  "closed_won",
];
const SERVER_OPPORTUNITY_TERMINAL_STAGES = new Set(["closed_won", "closed_lost"]);
const SERVER_OPPORTUNITY_OFF_RAMP = "closed_lost";

describe("D8: client mirror matches the server exactly", () => {
  it("LEADS_PIPELINE.stageOrder matches leads/pipeline.py STAGE_ORDER", () => {
    expect([...LEADS_PIPELINE.stageOrder]).toEqual(SERVER_LEAD_STAGE_ORDER);
  });

  it("LEADS_PIPELINE.terminalStages matches leads/pipeline.py TERMINAL_STAGES", () => {
    expect(new Set(LEADS_PIPELINE.terminalStages)).toEqual(SERVER_LEAD_TERMINAL_STAGES);
  });

  it("LEADS_PIPELINE.offRamp matches leads/pipeline.py's disqualified off-ramp", () => {
    expect(LEADS_PIPELINE.offRamp).toBe(SERVER_LEAD_OFF_RAMP);
  });

  it("OPPORTUNITIES_PIPELINE.stageOrder matches opportunities/pipeline.py STAGE_ORDER", () => {
    expect([...OPPORTUNITIES_PIPELINE.stageOrder]).toEqual(SERVER_OPPORTUNITY_STAGE_ORDER);
  });

  it("OPPORTUNITIES_PIPELINE.terminalStages matches opportunities/pipeline.py TERMINAL_STAGES", () => {
    expect(new Set(OPPORTUNITIES_PIPELINE.terminalStages)).toEqual(
      SERVER_OPPORTUNITY_TERMINAL_STAGES
    );
  });

  it("OPPORTUNITIES_PIPELINE.offRamp matches opportunities/pipeline.py's closed_lost off-ramp", () => {
    expect(OPPORTUNITIES_PIPELINE.offRamp).toBe(SERVER_OPPORTUNITY_OFF_RAMP);
  });
});

// ---------------------------------------------------------------------------
// Exhaustive legalTargets() correctness -- EVERY stage of BOTH funnels.
// ---------------------------------------------------------------------------
describe("legalTargets: leads funnel (captured -> qualified -> contacted -> converted, off-ramp disqualified)", () => {
  // SR-18 follow-up: `converted` is now additionally gated on `currentRole`
  // being CLIENT_ADMIN (restrictedTargets), since dropping there calls the
  // CLIENT_ADMIN-only convert endpoint. This funnel-shape table documents
  // the TRUE next-stage mapping, so it passes `"CLIENT_ADMIN"` throughout to
  // keep asserting the full shape; the role-OMITTED (fail-closed) and
  // CLIENT_AGENT-excluded cases are covered separately below, in "SR-18
  // follow-up: converted is CLIENT_ADMIN-only, every other transition is
  // role-blind".
  it.each([
    ["captured", ["qualified", "disqualified"]],
    ["qualified", ["contacted", "disqualified"]],
    ["contacted", ["converted", "disqualified"]],
    ["converted", []],
    ["disqualified", []],
  ])("legalTargets(%s) === %j", (current, expected) => {
    expect(legalTargets(LEADS_PIPELINE, current, "CLIENT_ADMIN")).toEqual(expected);
  });
});

describe("legalTargets: opportunities funnel (prospecting -> qualification -> proposal -> negotiation -> closed_won, off-ramp closed_lost)", () => {
  it.each([
    ["prospecting", ["qualification", "closed_lost"]],
    ["qualification", ["proposal", "closed_lost"]],
    ["proposal", ["negotiation", "closed_lost"]],
    ["negotiation", ["closed_won", "closed_lost"]],
    ["closed_won", []],
    ["closed_lost", []],
  ])("legalTargets(%s) === %j", (current, expected) => {
    expect(legalTargets(OPPORTUNITIES_PIPELINE, current)).toEqual(expected);
  });
});

// ---------------------------------------------------------------------------
// Skips are never offered.
// ---------------------------------------------------------------------------
describe("skips are not offered", () => {
  it("leads: captured -> contacted is illegal", () => {
    expect(isLegalTarget(LEADS_PIPELINE, "captured", "contacted")).toBe(false);
  });
  it("leads: captured -> converted is illegal", () => {
    expect(isLegalTarget(LEADS_PIPELINE, "captured", "converted")).toBe(false);
  });
  it("opportunities: prospecting -> proposal is illegal", () => {
    expect(isLegalTarget(OPPORTUNITIES_PIPELINE, "prospecting", "proposal")).toBe(false);
  });
  it("opportunities: prospecting -> negotiation is illegal", () => {
    expect(isLegalTarget(OPPORTUNITIES_PIPELINE, "prospecting", "negotiation")).toBe(false);
  });
  it("opportunities: qualification -> closed_won is illegal", () => {
    expect(isLegalTarget(OPPORTUNITIES_PIPELINE, "qualification", "closed_won")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Backward moves are never offered.
// ---------------------------------------------------------------------------
describe("backward moves are not offered", () => {
  it("leads: contacted -> qualified is illegal", () => {
    expect(isLegalTarget(LEADS_PIPELINE, "contacted", "qualified")).toBe(false);
  });
  it("leads: qualified -> captured is illegal", () => {
    expect(isLegalTarget(LEADS_PIPELINE, "qualified", "captured")).toBe(false);
  });
  it("opportunities: proposal -> qualification is illegal", () => {
    expect(isLegalTarget(OPPORTUNITIES_PIPELINE, "proposal", "qualification")).toBe(false);
  });
  it("opportunities: negotiation -> prospecting is illegal", () => {
    expect(isLegalTarget(OPPORTUNITIES_PIPELINE, "negotiation", "prospecting")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// No-ops are never offered.
// ---------------------------------------------------------------------------
describe("no-ops are not offered", () => {
  it.each(LEADS_PIPELINE.stageOrder)("leads: %s -> itself is illegal", (stage) => {
    expect(isLegalTarget(LEADS_PIPELINE, stage, stage)).toBe(false);
  });
  it.each(OPPORTUNITIES_PIPELINE.stageOrder)("opportunities: %s -> itself is illegal", (stage) => {
    expect(isLegalTarget(OPPORTUNITIES_PIPELINE, stage, stage)).toBe(false);
  });
  it("leads: disqualified -> disqualified is illegal (terminal no-op)", () => {
    expect(isLegalTarget(LEADS_PIPELINE, "disqualified", "disqualified")).toBe(false);
  });
  it("opportunities: closed_lost -> closed_lost is illegal (terminal no-op)", () => {
    expect(isLegalTarget(OPPORTUNITIES_PIPELINE, "closed_lost", "closed_lost")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Terminal cards are not draggable at all (SR-9.4 D5 no-reopen).
// ---------------------------------------------------------------------------
describe("terminal cards are not draggable at all", () => {
  it("leads: converted is not draggable", () => {
    expect(isDraggable(LEADS_PIPELINE, "converted")).toBe(false);
  });
  it("leads: disqualified is not draggable", () => {
    expect(isDraggable(LEADS_PIPELINE, "disqualified")).toBe(false);
  });
  it("opportunities: closed_won is not draggable", () => {
    expect(isDraggable(OPPORTUNITIES_PIPELINE, "closed_won")).toBe(false);
  });
  it("opportunities: closed_lost is not draggable", () => {
    expect(isDraggable(OPPORTUNITIES_PIPELINE, "closed_lost")).toBe(false);
  });
  it("leads: non-terminal stages ARE draggable", () => {
    for (const stage of ["captured", "qualified", "contacted"]) {
      expect(isDraggable(LEADS_PIPELINE, stage)).toBe(true);
    }
  });
  it("opportunities: non-terminal stages ARE draggable", () => {
    for (const stage of ["prospecting", "qualification", "proposal", "negotiation"]) {
      expect(isDraggable(OPPORTUNITIES_PIPELINE, stage)).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// isOffRamp
// ---------------------------------------------------------------------------
describe("isOffRamp", () => {
  it("leads: disqualified is the off-ramp; nothing else is", () => {
    expect(isOffRamp(LEADS_PIPELINE, "disqualified")).toBe(true);
    for (const stage of LEADS_PIPELINE.stageOrder) {
      expect(isOffRamp(LEADS_PIPELINE, stage)).toBe(false);
    }
  });
  it("opportunities: closed_lost is the off-ramp; nothing else is", () => {
    expect(isOffRamp(OPPORTUNITIES_PIPELINE, "closed_lost")).toBe(true);
    for (const stage of OPPORTUNITIES_PIPELINE.stageOrder) {
      expect(isOffRamp(OPPORTUNITIES_PIPELINE, stage)).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// SR-18 follow-up: role-gated `converted` target.
//
// Dropping a lead on Converted now calls the real CLIENT_ADMIN-only convert
// endpoint (POST /admin/leads/{lead_id}/convert), not the bare PATCH. The
// user explicitly chose not to widen backend RBAC, so the frontend gates
// its own affordance via `LEADS_PIPELINE.restrictedTargets`. These tests
// pin: (1) CLIENT_ADMIN keeps seeing `converted` as legal from `contacted`,
// exactly as before this fix: (2) CLIENT_AGENT and no-role-supplied both
// lose it; (3) every OTHER leads transition, and everything about
// OPPORTUNITIES_PIPELINE, is completely unaffected by role -- this is the
// regression guard against role-gating leaking into any other column.
// ---------------------------------------------------------------------------
describe("SR-18 follow-up: converted is CLIENT_ADMIN-only, every other transition is role-blind", () => {
  it("CLIENT_ADMIN: legalTargets(contacted) still includes converted", () => {
    expect(legalTargets(LEADS_PIPELINE, "contacted", "CLIENT_ADMIN")).toEqual([
      "converted",
      "disqualified",
    ]);
  });

  it("CLIENT_AGENT: legalTargets(contacted) excludes converted but keeps disqualified", () => {
    expect(legalTargets(LEADS_PIPELINE, "contacted", "CLIENT_AGENT")).toEqual(["disqualified"]);
  });

  it("PLATFORM_ADMIN and VISITOR also do not get converted (only CLIENT_ADMIN does)", () => {
    expect(legalTargets(LEADS_PIPELINE, "contacted", "PLATFORM_ADMIN")).toEqual(["disqualified"]);
    expect(legalTargets(LEADS_PIPELINE, "contacted", "VISITOR")).toEqual(["disqualified"]);
  });

  it("omitting currentRole entirely excludes converted (fail-closed default)", () => {
    expect(legalTargets(LEADS_PIPELINE, "contacted")).toEqual(["disqualified"]);
  });

  it("isLegalTarget(contacted, converted) is true only for CLIENT_ADMIN", () => {
    expect(isLegalTarget(LEADS_PIPELINE, "contacted", "converted", "CLIENT_ADMIN")).toBe(true);
    expect(isLegalTarget(LEADS_PIPELINE, "contacted", "converted", "CLIENT_AGENT")).toBe(false);
    expect(isLegalTarget(LEADS_PIPELINE, "contacted", "converted")).toBe(false);
  });

  it("isLegalTarget(contacted, disqualified) is unaffected by role either way", () => {
    expect(isLegalTarget(LEADS_PIPELINE, "contacted", "disqualified", "CLIENT_ADMIN")).toBe(true);
    expect(isLegalTarget(LEADS_PIPELINE, "contacted", "disqualified", "CLIENT_AGENT")).toBe(true);
    expect(isLegalTarget(LEADS_PIPELINE, "contacted", "disqualified")).toBe(true);
  });

  it.each<[string, string]>([
    ["captured", "qualified"],
    ["captured", "disqualified"],
    ["qualified", "contacted"],
    ["qualified", "disqualified"],
  ])(
    "regression: every OTHER leads transition (%s -> %s) is identical for CLIENT_ADMIN, CLIENT_AGENT, and no role",
    (current, target) => {
      const admin = isLegalTarget(LEADS_PIPELINE, current, target, "CLIENT_ADMIN");
      const agent = isLegalTarget(LEADS_PIPELINE, current, target, "CLIENT_AGENT");
      const none = isLegalTarget(LEADS_PIPELINE, current, target);
      expect(admin).toBe(true);
      expect(agent).toBe(true);
      expect(none).toBe(true);
    }
  );

  it("regression: illegal leads transitions stay illegal for every role (role never WIDENS legality)", () => {
    for (const role of ["CLIENT_ADMIN", "CLIENT_AGENT", "PLATFORM_ADMIN", "VISITOR"] as const) {
      expect(isLegalTarget(LEADS_PIPELINE, "captured", "converted", role)).toBe(false);
      expect(isLegalTarget(LEADS_PIPELINE, "captured", "contacted", role)).toBe(false);
    }
  });

  it("regression: OPPORTUNITIES_PIPELINE (no restrictedTargets) is completely unaffected by currentRole", () => {
    for (const role of ["CLIENT_ADMIN", "CLIENT_AGENT", "PLATFORM_ADMIN", "VISITOR"] as const) {
      expect(legalTargets(OPPORTUNITIES_PIPELINE, "negotiation", role)).toEqual([
        "closed_won",
        "closed_lost",
      ]);
    }
    expect(legalTargets(OPPORTUNITIES_PIPELINE, "negotiation")).toEqual([
      "closed_won",
      "closed_lost",
    ]);
  });
});

// ---------------------------------------------------------------------------
// Exhaustiveness cross-check: for every stage of every funnel, legalTargets
// returns EXACTLY {next stage (if any, if non-terminal)} + {off-ramp (if
// non-terminal)} and nothing else -- not spot-checked.
// ---------------------------------------------------------------------------
describe("exhaustive cross-check across all stages of both funnels", () => {
  function allStages(pipeline: typeof LEADS_PIPELINE): string[] {
    return [...pipeline.stageOrder, ...pipeline.terminalStages].filter(
      (stage, index, arr) => arr.indexOf(stage) === index
    );
  }

  it.each(allStages(LEADS_PIPELINE))("leads stage %s", (stage) => {
    // SR-18 follow-up: pass "CLIENT_ADMIN" so this exhaustive check still
    // documents the true funnel shape (including converted); the role
    // gate itself is covered by the dedicated describe block above.
    const targets = legalTargets(LEADS_PIPELINE, stage, "CLIENT_ADMIN");
    if (LEADS_PIPELINE.terminalStages.has(stage)) {
      expect(targets).toEqual([]);
      return;
    }
    const idx = LEADS_PIPELINE.stageOrder.indexOf(stage);
    const expectedNext =
      idx + 1 < LEADS_PIPELINE.stageOrder.length ? [LEADS_PIPELINE.stageOrder[idx + 1]] : [];
    expect(targets).toEqual([...expectedNext, LEADS_PIPELINE.offRamp]);
  });

  it.each(allStages(OPPORTUNITIES_PIPELINE))("opportunities stage %s", (stage) => {
    const targets = legalTargets(OPPORTUNITIES_PIPELINE, stage);
    if (OPPORTUNITIES_PIPELINE.terminalStages.has(stage)) {
      expect(targets).toEqual([]);
      return;
    }
    const idx = OPPORTUNITIES_PIPELINE.stageOrder.indexOf(stage);
    const expectedNext =
      idx + 1 < OPPORTUNITIES_PIPELINE.stageOrder.length
        ? [OPPORTUNITIES_PIPELINE.stageOrder[idx + 1]]
        : [];
    expect(targets).toEqual([...expectedNext, OPPORTUNITIES_PIPELINE.offRamp]);
  });
});
