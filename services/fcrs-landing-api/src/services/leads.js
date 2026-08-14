import { getPool } from "../db/pool.js";

// Supabase Postgres replaces Google Sheets as the lead store. Same interface
// shape the route already depends on (isDuplicate / appendRow) so the
// orchestration order in estimate-request.js is unchanged.

const DEFAULT_DUPLICATE_WINDOW_MINUTES = 30;

export function createLeadsService({ env = process.env, pool } = {}) {
  const db = pool !== undefined ? pool : getPool(env);
  const duplicateWindowMinutes = Number.parseInt(
    env.DUPLICATE_WINDOW_MINUTES ?? String(DEFAULT_DUPLICATE_WINDOW_MINUTES),
    10
  );

  function requireDb() {
    if (!db) throw new Error("Lead database is not configured.");
    return db;
  }

  return {
    async isDuplicate(email, now = new Date()) {
      const client = requireDb();
      const { rows } = await client.query(
        `SELECT 1 FROM public.leads
          WHERE email = $1
            AND created_at >= $2::timestamptz - make_interval(mins => $3::int)
          LIMIT 1`,
        [email, now.toISOString(), duplicateWindowMinutes]
      );
      return rows.length > 0;
    },

    async appendRow(submission, now = new Date()) {
      const client = requireDb();
      await client.query(
        `INSERT INTO public.leads
           (created_at, name, phone, email, service, state, zip, preferred_date, notes)
         VALUES ($1::timestamptz, $2, $3, $4, $5, $6, $7, $8, $9)`,
        [
          now.toISOString(),
          submission.name,
          submission.phone,
          submission.email,
          submission.service,
          submission.state,
          submission.zip,
          submission.date ?? null,
          submission.notes ?? null,
        ]
      );
    },

    async ping() {
      const client = requireDb();
      await client.query("SELECT 1");
    },
  };
}
