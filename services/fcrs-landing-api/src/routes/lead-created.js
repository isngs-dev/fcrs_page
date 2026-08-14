import { Router } from "express";
import { timingSafeEqual } from "node:crypto";

// Receives the Supabase Database Webhook fired by an INSERT on public.leads
// and dispatches the two transactional emails. This runs on Supabase's
// infrastructure schedule, entirely independent of the visitor's browser —
// the whole reason emails are triggered here rather than client-side.
//
// Supabase (pg_net) payload shape:
//   { type: "INSERT" | "UPDATE" | "DELETE",
//     table: "leads", schema: "public",
//     record: {...} | null, old_record: {...} | null }

const WEBHOOK_HEADER = "x-webhook-secret";

function secretsMatch(provided, expected) {
  if (typeof provided !== "string" || typeof expected !== "string") return false;
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export function submissionFromRecord(record) {
  return {
    name: record.name,
    phone: record.phone,
    email: record.email,
    service: record.service,
    state: record.state,
    zip: record.zip,
    date: record.preferred_date ?? undefined,
    notes: record.notes ?? undefined,
  };
}

export function createLeadCreatedRouter({ emailService, env = process.env }) {
  const router = Router();
  const expectedSecret = env.LEAD_WEBHOOK_SECRET;

  router.post("/internal/lead-created", async (req, res) => {
    if (!expectedSecret) {
      console.warn("Lead webhook rejected: LEAD_WEBHOOK_SECRET is not configured.");
      return res.status(503).json({ ok: false });
    }

    if (!secretsMatch(req.get(WEBHOOK_HEADER), expectedSecret)) {
      console.warn("Lead webhook rejected: invalid or missing webhook secret.");
      return res.status(401).json({ ok: false });
    }

    const payload = req.body || {};
    if (payload.type !== "INSERT" || payload.table !== "leads" || !payload.record) {
      console.warn(`Lead webhook ignored: unexpected event type "${payload.type}".`);
      return res.status(200).json({ ok: true, ignored: true });
    }

    const submission = submissionFromRecord(payload.record);
    const timestamp = payload.record.created_at || new Date().toISOString();

    try {
      await emailService.sendLeadEmails(submission, { timestamp });
    } catch (err) {
      console.warn(`Lead webhook email dispatch failed: ${err && err.message ? err.message : "unknown error"}`);
    }

    return res.status(200).json({ ok: true });
  });

  return router;
}
