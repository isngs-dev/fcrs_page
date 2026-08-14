import { describe, it, expect, vi } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";

const testEnv = {
  ALLOWED_ORIGIN: "http://localhost:4321",
  LEAD_WEBHOOK_SECRET: "test-webhook-secret",
};

const samplePayload = {
  type: "INSERT",
  table: "leads",
  schema: "public",
  old_record: null,
  record: {
    id: 1,
    created_at: "2026-07-25T12:00:00.000Z",
    name: "Jane Smith",
    phone: "(989) 843-4628",
    email: "jane@example.com",
    service: "Residential Roofing",
    state: "Alabama",
    zip: "35203",
    preferred_date: "2026-08-01T10:00",
    notes: "Leak near the chimney.",
  },
};

function createFakeLeadsService() {
  return {
    async isDuplicate() {
      return false;
    },
    async appendRow() {},
    async ping() {},
  };
}

function createFakeEmailService({ throwOnSend = false } = {}) {
  return {
    sendLeadEmails: vi.fn(async () => {
      if (throwOnSend) throw new Error("emailjs down");
    }),
  };
}

describe("POST /internal/lead-created", () => {
  it("returns 401 and never dispatches email when the secret header is missing", async () => {
    const emailService = createFakeEmailService();
    const app = createApp({ env: testEnv, leadsService: createFakeLeadsService(), emailService });

    const res = await request(app).post("/internal/lead-created").send(samplePayload);

    expect(res.status).toBe(401);
    expect(emailService.sendLeadEmails).not.toHaveBeenCalled();
  });

  it("returns 401 and never dispatches email when the secret header is wrong", async () => {
    const emailService = createFakeEmailService();
    const app = createApp({ env: testEnv, leadsService: createFakeLeadsService(), emailService });

    const res = await request(app)
      .post("/internal/lead-created")
      .set("x-webhook-secret", "wrong-secret")
      .send(samplePayload);

    expect(res.status).toBe(401);
    expect(emailService.sendLeadEmails).not.toHaveBeenCalled();
  });

  it("returns 200 and dispatches email once with the record mapped to submission shape on a valid INSERT", async () => {
    const emailService = createFakeEmailService();
    const app = createApp({ env: testEnv, leadsService: createFakeLeadsService(), emailService });

    const res = await request(app)
      .post("/internal/lead-created")
      .set("x-webhook-secret", "test-webhook-secret")
      .send(samplePayload);

    expect(res.status).toBe(200);
    expect(emailService.sendLeadEmails).toHaveBeenCalledTimes(1);
    const [submission, options] = emailService.sendLeadEmails.mock.calls[0];
    expect(submission).toEqual({
      name: "Jane Smith",
      phone: "(989) 843-4628",
      email: "jane@example.com",
      service: "Residential Roofing",
      state: "Alabama",
      zip: "35203",
      date: "2026-08-01T10:00",
      notes: "Leak near the chimney.",
    });
    expect(options).toEqual({ timestamp: "2026-07-25T12:00:00.000Z" });
  });

  it("returns 200 (ACK) but does not dispatch email for a non-INSERT event type", async () => {
    const emailService = createFakeEmailService();
    const app = createApp({ env: testEnv, leadsService: createFakeLeadsService(), emailService });

    const res = await request(app)
      .post("/internal/lead-created")
      .set("x-webhook-secret", "test-webhook-secret")
      .send({ ...samplePayload, type: "UPDATE" });

    expect(res.status).toBe(200);
    expect(emailService.sendLeadEmails).not.toHaveBeenCalled();
  });

  it("still returns 200 when sendLeadEmails throws, and never logs the submission's email or payload", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const emailService = createFakeEmailService({ throwOnSend: true });
    const app = createApp({ env: testEnv, leadsService: createFakeLeadsService(), emailService });

    const res = await request(app)
      .post("/internal/lead-created")
      .set("x-webhook-secret", "test-webhook-secret")
      .send(samplePayload);

    expect(res.status).toBe(200);
    expect(warnSpy).toHaveBeenCalled();
    const loggedText = warnSpy.mock.calls.map((c) => c.join(" ")).join(" ");
    expect(loggedText).not.toContain("jane@example.com");
    expect(loggedText).not.toContain("Jane Smith");
    expect(loggedText).not.toContain("35203");

    warnSpy.mockRestore();
  });
});
