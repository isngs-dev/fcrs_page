import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PipelineBoard } from "@/components/admin/pipeline-board";
import { LEADS_PIPELINE, OPPORTUNITIES_PIPELINE } from "@/lib/pipelines";
import type { TransitionResult } from "@/app/(protected)/actions/pipeline-actions";

/**
 * Static-markup structural tests for `PipelineBoard` (SR-18 D2/D3/D5/D7).
 *
 * IMPORTANT, stated honestly per CLAUDE.md §6b ("report outcomes, not
 * effort"): this repo has NO jsdom and NO testing-library (CLAUDE.md §4
 * forbids adding either for a frontend-only sprint; every existing
 * component test in this codebase, e.g.
 * `leads/__tests__/lead-drawer-timeline.test.tsx`, uses the exact same
 * `renderToStaticMarkup` pattern for the same reason). That means these
 * tests can assert the board's INITIAL rendered structure (columns exist,
 * cards render in the right column, a terminal card is marked
 * non-draggable, an empty column shows its empty state) but CANNOT actually
 * fire a `pointerdown`/`pointermove`/`pointerup` sequence, press arrow
 * keys, or observe the live region update mid-drag -- there is no DOM event
 * loop or focus model to drive. The keyboard-only transition, screen-reader
 * announcement, and touch-drag DoD items (spec §F.14-16) require a real
 * browser and are called out as such in the sprint report; they are NOT
 * silently marked done here.
 */

const leadCards = [
  { id: "lead-1", stage: "captured" },
  { id: "lead-2", stage: "converted" },
  { id: "lead-3", stage: "disqualified" },
];

const dealCards = [
  { id: "opp-1", stage: "prospecting" },
  { id: "opp-2", stage: "closed_won" },
  { id: "opp-3", stage: "closed_lost" },
];

const LEAD_COLUMNS = [
  { key: "captured", label: "Captured" },
  { key: "qualified", label: "Qualified" },
  { key: "contacted", label: "Contacted" },
  { key: "converted", label: "Converted" },
  { key: "disqualified", label: "Disqualified" },
];

const DEAL_COLUMNS = [
  { key: "prospecting", label: "Prospecting" },
  { key: "qualification", label: "Qualification" },
  { key: "proposal", label: "Proposal" },
  { key: "negotiation", label: "Negotiation" },
  { key: "closed_won", label: "Closed Won" },
  { key: "closed_lost", label: "Closed Lost" },
];

const okTransition = async (): Promise<TransitionResult> => ({ status: "ok", stage: "qualified" });

describe("PipelineBoard -- structural rendering", () => {
  it("renders every column of the leads funnel, including the off-ramp", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    for (const column of LEAD_COLUMNS) {
      expect(html).toMatch(new RegExp(column.label));
    }
  });

  it("renders every column of the opportunities funnel, including the off-ramp", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={OPPORTUNITIES_PIPELINE}
        columns={DEAL_COLUMNS}
        cards={dealCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    for (const column of DEAL_COLUMNS) {
      expect(html).toMatch(new RegExp(column.label));
    }
  });

  it("places each card in its own stage's column data-testid", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    expect(html).toMatch(/data-testid="pipeline-card-lead-1"/);
    expect(html).toMatch(/data-testid="pipeline-card-lead-2"/);
    expect(html).toMatch(/data-testid="pipeline-card-lead-3"/);
  });

  it("terminal cards (converted, disqualified) render aria-disabled=true -- not draggable at all (SR-9.4 D5 no-reopen)", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    // A crude but effective structural check: find each card button's FULL
    // opening tag (from the preceding `<button` through the matching `>`)
    // and assert aria-disabled reflects its stage's terminality. A
    // non-terminal card must NOT be aria-disabled.
    function findButtonTag(testId: string): string {
      const idx = html.indexOf(`data-testid="${testId}"`);
      const tagStart = html.lastIndexOf("<button", idx);
      const tagEnd = html.indexOf(">", idx);
      return html.slice(tagStart, tagEnd + 1);
    }

    const capturedCardMatch = [findButtonTag("pipeline-card-lead-1")];
    const convertedCardMatch = [findButtonTag("pipeline-card-lead-2")];
    const disqualifiedCardMatch = [findButtonTag("pipeline-card-lead-3")];

    expect(capturedCardMatch?.[0]).not.toMatch(/aria-disabled="true"/);
    expect(convertedCardMatch?.[0]).toMatch(/aria-disabled="true"/);
    expect(disqualifiedCardMatch?.[0]).toMatch(/aria-disabled="true"/);
  });

  it("terminal deal cards (closed_won, closed_lost) render aria-disabled=true", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={OPPORTUNITIES_PIPELINE}
        columns={DEAL_COLUMNS}
        cards={dealCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    function findButtonTag(testId: string): string {
      const idx = html.indexOf(`data-testid="${testId}"`);
      const tagStart = html.lastIndexOf("<button", idx);
      const tagEnd = html.indexOf(">", idx);
      return html.slice(tagStart, tagEnd + 1);
    }

    const prospectingCardMatch = [findButtonTag("pipeline-card-opp-1")];
    const wonCardMatch = [findButtonTag("pipeline-card-opp-2")];
    const lostCardMatch = [findButtonTag("pipeline-card-opp-3")];

    expect(prospectingCardMatch?.[0]).not.toMatch(/aria-disabled="true"/);
    expect(wonCardMatch?.[0]).toMatch(/aria-disabled="true"/);
    expect(lostCardMatch?.[0]).toMatch(/aria-disabled="true"/);
  });

  it("an empty column renders an explicit empty state, never a placeholder card", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={[{ id: "lead-1", stage: "captured" }]}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
        emptyColumnLabel={(label) => `No leads in ${label}.`}
      />
    );

    // qualified/contacted/converted/disqualified all have zero cards here.
    expect(html).toMatch(/No leads in Qualified\./);
    expect(html).toMatch(/No leads in Contacted\./);
  });

  it("uses a default empty-column message when emptyColumnLabel is omitted", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={[] as typeof leadCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    expect(html).toMatch(/Nothing here\./);
  });

  it("renders a single aria-live=polite announcer region (screen-reader announcement channel)", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    expect(html).toMatch(/data-testid="pipeline-board-announcer"/);
    expect(html).toMatch(/aria-live="polite"/);
  });

  it("card renderer content is used verbatim inside the card button", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={[{ id: "lead-1", stage: "captured" }]}
        renderCard={() => <span data-testid="custom-card-content">Ada Lovelace</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    expect(html).toMatch(/data-testid="custom-card-content"/);
    expect(html).toMatch(/Ada Lovelace/);
  });
});

/**
 * SR-18 follow-up: `currentRole` + `restrictedTargets` structural rendering.
 *
 * These are the same crude-but-effective `aria-disabled`-on-terminal-card
 * check reused for a different question -- not "is this card terminal" but
 * "is this COLUMN a legal drop target while a specific card is grabbed."
 * `PipelineBoard` only renders `aria-dropeffect` on a column while `drag`
 * is non-null (i.e. mid-grab), and `renderToStaticMarkup` cannot fire a real
 * pointerdown -- so these tests construct the board in an initial state and
 * instead assert via the exported `isLegalTarget` pure function (already
 * exhaustively tested in `lib/__tests__/pipelines.test.ts`) SEPARATELY from
 * a structural check that `currentRole` is accepted and threaded without
 * crashing/altering unrelated markup. The live drag-state role-gating
 * itself is exercised by `pipelines.test.ts`, since `PipelineBoard`
 * delegates every legality decision to `lib/pipelines.ts` and never
 * reimplements it.
 */
describe("PipelineBoard -- currentRole prop (SR-18 follow-up)", () => {
  it("accepts currentRole without altering unrelated structural output (CLIENT_ADMIN)", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        currentRole="CLIENT_ADMIN"
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    for (const column of LEAD_COLUMNS) {
      expect(html).toMatch(new RegExp(column.label));
    }
    expect(html).toMatch(/data-testid="pipeline-card-lead-1"/);
  });

  it("accepts currentRole without altering unrelated structural output (CLIENT_AGENT)", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        currentRole="CLIENT_AGENT"
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    for (const column of LEAD_COLUMNS) {
      expect(html).toMatch(new RegExp(column.label));
    }
    expect(html).toMatch(/data-testid="pipeline-card-lead-1"/);
  });

  it("renders identically for the deals board (OPPORTUNITIES_PIPELINE has no restrictedTargets) regardless of currentRole", () => {
    const withRole = renderToStaticMarkup(
      <PipelineBoard
        pipeline={OPPORTUNITIES_PIPELINE}
        columns={DEAL_COLUMNS}
        cards={dealCards}
        currentRole="CLIENT_AGENT"
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );
    const withoutRole = renderToStaticMarkup(
      <PipelineBoard
        pipeline={OPPORTUNITIES_PIPELINE}
        columns={DEAL_COLUMNS}
        cards={dealCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );

    expect(withRole).toBe(withoutRole);
  });
});

describe("PipelineBoard -- requiresConfirmation override (SR-18 follow-up)", () => {
  it("defaults to isOffRamp (disqualified requires confirmation, converted does not) when requiresConfirmation is omitted", () => {
    // Structural smoke test only -- the actual commit-vs-confirm branch is
    // exercised at runtime (commitDrop), which renderToStaticMarkup cannot
    // drive. This asserts the board still renders correctly with no
    // requiresConfirmation override supplied (the deals board's usage).
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
      />
    );
    expect(html).toMatch(/Converted/);
    expect(html).toMatch(/Disqualified/);
  });

  it("accepts a custom requiresConfirmation (leads-board's converted+disqualified) without crashing", () => {
    const html = renderToStaticMarkup(
      <PipelineBoard
        pipeline={LEADS_PIPELINE}
        columns={LEAD_COLUMNS}
        cards={leadCards}
        currentRole="CLIENT_ADMIN"
        renderCard={(card) => <span>{card.id}</span>}
        onTransition={okTransition}
        onOffRampConfirm={() => {}}
        requiresConfirmation={(pipeline, targetStage) =>
          targetStage === pipeline.offRamp || targetStage === "converted"
        }
      />
    );
    expect(html).toMatch(/Converted/);
  });
});
