import { describe, it, expect } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";

const validPayload = {
  name: "Jane Smith",
  phone: "(989) 843-4628",
  email: "jane@example.com",
  service: "Residential Roofing",
  state: "Alabama",
  zip: "35203",
};

const testEnv = {
  ALLOWED_ORIGIN: "http://localhost:4321",
  DUPLICATE_WINDOW_MINUTES: "30",
};

/** Builds an in-memory fake leads service backed by an array of rows. */
function createFakeLeadsService({ throwOnAppend = false, throwOnDuplicateCheck = false } = {}) {
  const rows = [];
  return {
    rows,
    async isDuplicate(email, now = new Date()) {
      if (throwOnDuplicateCheck) throw new Error("read failed");
      const windowMs = 30 * 60 * 1000;
      return rows.some((row) => {
        const sameEmail = row.email.trim().toLowerCase() === email;
        const withinWindow = Math.abs(now.getTime() - new Date(row.timestamp).getTime()) <= windowMs;
        return sameEmail && withinWindow;
      });
    },
    async appendRow(submission, now = new Date()) {
      if (throwOnAppend) throw new Error("append failed");
      rows.push({ ...submission, timestamp: now.toISOString() });
    },
    async ping() {},
  };
}

describe("POST /api/estimate-request", () => {
  it("returns 200 ok:true and appends one row on a valid submission, without invoking any email collaborator", async () => {
    const leadsService = createFakeLeadsService();
    const app = createApp({ env: testEnv, leadsService });

    const res = await request(app).post("/api/estimate-request").send(validPayload);

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ ok: true });
    expect(leadsService.rows).toHaveLength(1);
    // No email-sending collaborator is injected into this route at all —
    // emails are dispatched only by the Supabase webhook (lead-created.js).
  });

  it("returns 400 with field errors and performs no writes on invalid input", async () => {
    const leadsService = createFakeLeadsService();
    const app = createApp({ env: testEnv, leadsService });

    const res = await request(app)
      .post("/api/estimate-request")
      .send({ ...validPayload, email: "not-an-email", zip: "" });

    expect(res.status).toBe(400);
    expect(res.body.ok).toBe(false);
    expect(res.body.errors).toHaveProperty("email");
    expect(res.body.errors).toHaveProperty("zip");
    expect(leadsService.rows).toHaveLength(0);
  });

  it("returns 500 when the lead insert throws", async () => {
    const leadsService = createFakeLeadsService({ throwOnAppend: true });
    const app = createApp({ env: testEnv, leadsService });

    const res = await request(app).post("/api/estimate-request").send(validPayload);

    expect(res.status).toBe(500);
    expect(res.body.ok).toBe(false);
    expect(res.body.error).toMatch(/something went wrong/i);
  });

  it("still captures the lead when the duplicate-check read fails", async () => {
    const leadsService = createFakeLeadsService({ throwOnDuplicateCheck: true });
    const app = createApp({ env: testEnv, leadsService });

    const res = await request(app).post("/api/estimate-request").send(validPayload);

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ ok: true });
    expect(leadsService.rows).toHaveLength(1);
  });

  it("collapses a same-email resubmit within the window into one row, still 200", async () => {
    const leadsService = createFakeLeadsService();
    const app = createApp({ env: testEnv, leadsService });

    const first = await request(app).post("/api/estimate-request").send(validPayload);
    const second = await request(app).post("/api/estimate-request").send(validPayload);

    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    expect(second.body).toEqual({ ok: true });
    expect(leadsService.rows).toHaveLength(1);
  });

  it("never collapses two different emails", async () => {
    const leadsService = createFakeLeadsService();
    const app = createApp({ env: testEnv, leadsService });

    await request(app).post("/api/estimate-request").send(validPayload);
    await request(app)
      .post("/api/estimate-request")
      .send({ ...validPayload, email: "someone.else@example.com" });

    expect(leadsService.rows).toHaveLength(2);
  });

  it("treats a same-email submission outside the window as a new submission", async () => {
    const leadsService = createFakeLeadsService();
    const app = createApp({ env: testEnv, leadsService });

    // Seed a row 45 minutes in the past — outside the 30-minute window.
    const past = new Date(Date.now() - 45 * 60 * 1000);
    await leadsService.appendRow(
      { name: "Jane Smith", phone: "555", email: "jane@example.com", service: "Residential Roofing", state: "Alabama", zip: "35203" },
      past
    );

    const res = await request(app).post("/api/estimate-request").send(validPayload);

    expect(res.status).toBe(200);
    expect(leadsService.rows).toHaveLength(2);
  });

  it("health check responds ok without touching leads", async () => {
    const leadsService = createFakeLeadsService();
    const app = createApp({ env: testEnv, leadsService });

    const res = await request(app).get("/api/health");

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ ok: true });
    expect(leadsService.rows).toHaveLength(0);
  });

  it("db health check responds ok when ping succeeds", async () => {
    const leadsService = createFakeLeadsService();
    const app = createApp({ env: testEnv, leadsService });

    const res = await request(app).get("/api/health/db");

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ ok: true });
  });

  it("db health check responds 503 when ping throws", async () => {
    const leadsService = createFakeLeadsService();
    leadsService.ping = async () => {
      throw new Error("connection refused");
    };
    const app = createApp({ env: testEnv, leadsService });

    const res = await request(app).get("/api/health/db");

    expect(res.status).toBe(503);
    expect(res.body).toEqual({ ok: false });
  });
});
