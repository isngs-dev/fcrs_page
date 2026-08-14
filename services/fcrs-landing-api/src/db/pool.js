import pg from "pg";

// Supabase Postgres is the ONLY lead data store. Plain `pg` — not
// @supabase/supabase-js — because this is a single-table CRUD need from a
// long-running Express process: no PostgREST/HTTP layer, connects as the
// table-owning role so RLS is bypassed cleanly, and trivially mockable.

let cachedPool = null;

export function getPool(env = process.env) {
  if (cachedPool) return cachedPool;

  const connectionString = env.DATABASE_URL;
  if (!connectionString) return null;

  cachedPool = new pg.Pool({
    connectionString,
    max: 5,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 10_000,
    // Supabase requires TLS. The pooler presents a cert chain Node does not
    // ship a root for; verification is relaxed while transport stays encrypted.
    ssl: { rejectUnauthorized: false },
  });

  cachedPool.on("error", (err) => {
    console.warn(`Postgres pool error: ${err && err.message ? err.message : "unknown error"}`);
  });

  return cachedPool;
}

export function resetPoolForTests() {
  cachedPool = null;
}
