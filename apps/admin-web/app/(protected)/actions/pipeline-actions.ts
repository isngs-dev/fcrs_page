"use server";

/**
 * SR-18 scope item 10: server actions for BOTH the leads and deals stage
 * transitions, the console's mutation convention (server actions, not a
 * client-side fetch to a REST route). Called directly (as a plain async
 * function, `await transitionLeadStage(...)`) from `pipeline-board.tsx`'s
 * drop handler and from the off-ramp confirmation dialog -- NOT wired
 * through `useActionState`/`<form action>`, because a drag-and-drop drop
 * needs a promise it can await inside its own pending-state management (the
 * board owns "in flight" per card, D5), not a form submission.
 *
 * Both actions are intentionally thin: Zod-validate the shape locally (a
 * courtesy pre-check, matching `contacts/actions.ts`'s posture), then POST/
 * PATCH straight through with a MINIMAL body -- **never** a client-computed
 * `status` or `qualification_score` for leads (M1: the server always derives
 * both), and the real `close_reason` for a deal's `closed_lost` transition
 * (M3/D9). Neither action calls `PUT /admin/opportunities/config` (M10 --
 * that endpoint is CLIENT_ADMIN-only and out of scope for this sprint).
 *
 * The client-side pipeline mirror (`lib/pipelines.ts`) is NEVER consulted
 * here -- these actions do not pre-validate the transition against
 * `legalTargets`. The server's own `validate_transition` (leads/pipeline.py,
 * opportunities/pipeline.py) is the sole enforcement point (D2/D8); this
 * file's only job is to shape the request and translate the response/error
 * for the board to render (D5's pessimistic, honest failure path).
 */
import { revalidatePath } from "next/cache";
import { z } from "zod";
import { AdminApiError, adminApiFetch } from "@/lib/api";

// ---------------------------------------------------------------------------
// Shared result shape -- the board's drop handler awaits one of these and
// either moves the card (ok) or reverts it with the real message (error).
// Never a boolean, never a silent throw swallowed into a snap-back (D5).
// ---------------------------------------------------------------------------

export interface TransitionOkResult {
  status: "ok";
  /** The server's own returned entity (integration requirement: the board
   * reflects the server's response, never a locally-mutated copy). */
  stage: string;
}

export interface TransitionErrorResult {
  status: "error";
  message: string;
  errorCode: string;
  correlationId: string;
}

export type TransitionResult = TransitionOkResult | TransitionErrorResult;

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

// ---------------------------------------------------------------------------
// Leads: PATCH /admin/leads/{lead_id} {stage}
// ---------------------------------------------------------------------------

const leadTransitionSchema = z.object({
  leadId: z.string().min(1),
  stage: z.string().min(1),
});

function mapLeadTransitionError(error: AdminApiError): string {
  if (error.errorCode === "INVALID_STAGE_TRANSITION") {
    return error.message || "That move isn't allowed for this lead's current stage.";
  }
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This lead could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to move this lead.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `${error.message} (correlation ID: ${error.correlationId || "unknown"}).`;
}

/**
 * Transition a lead to a new stage. Sends `{stage}` ONLY -- `status` and
 * `qualification_score` are always derived server-side (M1) and are never
 * accepted as input by this action or the endpoint it calls.
 */
export async function transitionLeadStage(
  leadId: string,
  targetStage: string,
  revalidatePathTarget: string
): Promise<TransitionResult> {
  const parsed = leadTransitionSchema.safeParse({ leadId, stage: targetStage });
  if (!parsed.success) {
    return {
      status: "error",
      message: "Invalid lead transition request.",
      errorCode: "INVALID_REQUEST",
      correlationId: "",
    };
  }

  try {
    const response = await adminApiFetch(`/admin/leads/${encodeURIComponent(leadId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: targetStage }),
    });
    const body = (await response.json()) as { stage: string };
    revalidatePath(revalidatePathTarget);
    return { status: "ok", stage: body.stage };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: mapLeadTransitionError(error),
        errorCode: error.errorCode,
        correlationId: error.correlationId,
      };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, errorCode: "NETWORK_ERROR", correlationId: "" };
  }
}

// ---------------------------------------------------------------------------
// Leads: POST /admin/leads/{lead_id}/convert {} -- SR-18 follow-up.
//
// The REAL conversion flow (creates a Contact, sets
// `converted_to_contact_id`), distinct from the bare stage PATCH above. The
// board's Converted column now calls this instead of `transitionLeadStage`
// when a card is dropped there. This endpoint is CLIENT_ADMIN-only
// (services/api/src/api/leads/admin_routes.py:910) -- the PATCH endpoint
// above allows CLIENT_ADMIN+CLIENT_AGENT, so the two intentionally diverge.
// No account linkage is offered from the board drop (D3 of SR-9.2: an
// Account is always optional) -- this sends an empty body.
// ---------------------------------------------------------------------------

function mapLeadConvertError(error: AdminApiError): string {
  if (error.status === 409 || error.errorCode === "LEAD_ALREADY_CONVERTED") {
    return "This lead has already been converted to a contact.";
  }
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This lead could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to convert this lead.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `${error.message} (correlation ID: ${error.correlationId || "unknown"}).`;
}

/**
 * Convert a lead into a Contact via the real convert endpoint. Returns the
 * same `TransitionResult` shape the board already knows how to render --
 * `stage: "converted"` is reported locally on success (D5 of this fix: the
 * board only needs to reflect the lead's new stage, never the created
 * Contact, matching every other column's post-transition behavior). The
 * response's real `contact_id` is intentionally not surfaced to the board.
 */
export async function convertLeadToContact(
  leadId: string,
  revalidatePathTarget: string
): Promise<TransitionResult> {
  const parsed = z.object({ leadId: z.string().min(1) }).safeParse({ leadId });
  if (!parsed.success) {
    return {
      status: "error",
      message: "Invalid lead conversion request.",
      errorCode: "INVALID_REQUEST",
      correlationId: "",
    };
  }

  try {
    await adminApiFetch(`/admin/leads/${encodeURIComponent(leadId)}/convert`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    revalidatePath(revalidatePathTarget);
    return { status: "ok", stage: "converted" };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: mapLeadConvertError(error),
        errorCode: error.errorCode,
        correlationId: error.correlationId,
      };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, errorCode: "NETWORK_ERROR", correlationId: "" };
  }
}

// ---------------------------------------------------------------------------
// Deals: POST /admin/opportunities/{opportunity_id}/stage {stage, close_reason?}
// ---------------------------------------------------------------------------

const dealTransitionSchema = z.object({
  opportunityId: z.string().min(1),
  stage: z.string().min(1),
  closeReason: z.string().trim().min(1).optional(),
});

function mapDealTransitionError(error: AdminApiError): string {
  if (error.errorCode === "CLOSE_REASON_REQUIRED") {
    return "A reason is required to mark this deal lost.";
  }
  if (error.errorCode === "INVALID_OPPORTUNITY_STAGE_TRANSITION") {
    return error.message || "That move isn't allowed for this deal's current stage.";
  }
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This deal could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to move this deal.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `${error.message} (correlation ID: ${error.correlationId || "unknown"}).`;
}

/**
 * Transition a deal to a new stage. `closeReason` is REQUIRED (client-side,
 * belt) when `targetStage === "closed_lost"` -- the server also enforces
 * this (suspenders, 422 `CLOSE_REASON_REQUIRED`, D4/M3) so both paths are
 * exercised regardless of which layer a caller manages to bypass.
 */
export async function transitionDealStage(
  opportunityId: string,
  targetStage: string,
  closeReason: string | undefined,
  revalidatePathTarget: string
): Promise<TransitionResult> {
  if (targetStage === "closed_lost" && (!closeReason || !closeReason.trim())) {
    return {
      status: "error",
      message: "A reason is required to mark this deal lost.",
      errorCode: "CLOSE_REASON_REQUIRED",
      correlationId: "",
    };
  }

  const parsed = dealTransitionSchema.safeParse({
    opportunityId,
    stage: targetStage,
    closeReason,
  });
  if (!parsed.success) {
    return {
      status: "error",
      message: "Invalid deal transition request.",
      errorCode: "INVALID_REQUEST",
      correlationId: "",
    };
  }

  try {
    const response = await adminApiFetch(
      `/admin/opportunities/${encodeURIComponent(opportunityId)}/stage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          stage: targetStage,
          close_reason: closeReason && closeReason.trim() ? closeReason.trim() : null,
        }),
      }
    );
    const body = (await response.json()) as { stage: string };
    revalidatePath(revalidatePathTarget);
    return { status: "ok", stage: body.stage };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: mapDealTransitionError(error),
        errorCode: error.errorCode,
        correlationId: error.correlationId,
      };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, errorCode: "NETWORK_ERROR", correlationId: "" };
  }
}
