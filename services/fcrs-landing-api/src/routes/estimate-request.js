import { Router } from "express";
import { estimateRequestSchema } from "../schema/estimate-request.js";

const GENERIC_SERVER_ERROR = {
  ok: false,
  error: "Something went wrong. Please call us at the number above.",
};

/**
 * Converts zod issues into the flat `{ field: message }` map required by the
 * response contract. Human-facing prose only, never raw zod codes, and
 * unknown-key issues never echo the offending key name back.
 */
function formatZodErrors(zodError) {
  const errors = {};
  for (const issue of zodError.issues) {
    const key = issue.path[0];
    if (typeof key !== "string") continue;
    if (!(key in errors)) {
      errors[key] = issue.message;
    }
  }
  return errors;
}

/**
 * Builds the /api/estimate-request route. The leads collaborator (Supabase
 * Postgres-backed) is passed in through a factory/params object so tests can
 * substitute a mock — nothing here imports `pg` directly.
 *
 * Emails are NO LONGER dispatched from this handler. They are sent by the
 * Supabase Database Webhook (POST /internal/lead-created, see
 * routes/lead-created.js) fired server-side by Postgres on INSERT, so
 * delivery never depends on this request/response cycle or the visitor's
 * browser staying open.
 */
export function createEstimateRequestRouter({ leadsService }) {
  const router = Router();

  router.post("/api/estimate-request", async (req, res) => {
    const parseResult = estimateRequestSchema.safeParse(req.body);

    if (!parseResult.success) {
      return res.status(400).json({ ok: false, errors: formatZodErrors(parseResult.error) });
    }

    const submission = parseResult.data;
    const now = new Date();

    try {
      const duplicate = await leadsService.isDuplicate(submission.email, now);
      if (duplicate) {
        return res.status(200).json({ ok: true });
      }
    } catch (err) {
      console.warn(`Duplicate check failed: ${err && err.message ? err.message : "unknown error"}`);
      // Duplicate suppression is protective, but it must not prevent the
      // primary lead capture. If the read path is temporarily unavailable,
      // continue to the append and accept the small risk of a duplicate row.
    }

    try {
      await leadsService.appendRow(submission, now);
    } catch (err) {
      console.warn(`Lead insert failed: ${err && err.message ? err.message : "unknown error"}`);
      return res.status(500).json(GENERIC_SERVER_ERROR);
    }

    return res.status(200).json({ ok: true });
  });

  return router;
}
