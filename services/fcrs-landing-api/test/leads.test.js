import { describe, it, expect, vi } from "vitest";
import { createLeadsService } from "../src/services/leads.js";

const baseEnv = { DUPLICATE_WINDOW_MINUTES: "30" };

const validSubmission = {
  name: "Jane Smith",
  phone: "(989) 843-4628",
  email: "jane@example.com",
  service: "Residential Roofing",
  state: "Alabama",
  zip: "35203",
};

describe("createLeadsService", () => {
  it("isDuplicate returns true when the query finds a matching row", async () => {
    const fakePool = { query: vi.fn().mockResolvedValue({ rows: [{ "?column?": 1 }] }) };
    const leadsService = createLeadsService({ env: baseEnv, pool: fakePool });

    const result = await leadsService.isDuplicate("jane@example.com");

    expect(result).toBe(true);
  });

  it("isDuplicate returns false when the query finds no matching row", async () => {
    const fakePool = { query: vi.fn().mockResolvedValue({ rows: [] }) };
    const leadsService = createLeadsService({ env: baseEnv, pool: fakePool });

    const result = await leadsService.isDuplicate("jane@example.com");

    expect(result).toBe(false);
  });

  it("passes email, ISO timestamp, and the configured window as query params", async () => {
    const fakePool = { query: vi.fn().mockResolvedValue({ rows: [] }) };
    const leadsService = createLeadsService({ env: { DUPLICATE_WINDOW_MINUTES: "45" }, pool: fakePool });
    const now = new Date("2026-07-25T12:00:00.000Z");

    await leadsService.isDuplicate("jane@example.com", now);

    expect(fakePool.query).toHaveBeenCalledTimes(1);
    const [sql, params] = fakePool.query.mock.calls[0];
    expect(sql).toMatch(/SELECT 1 FROM public\.leads/);
    expect(params).toEqual(["jane@example.com", "2026-07-25T12:00:00.000Z", 45]);
  });

  it("appendRow sends 9 params in the correct column order", async () => {
    const fakePool = { query: vi.fn().mockResolvedValue({ rows: [] }) };
    const leadsService = createLeadsService({ env: baseEnv, pool: fakePool });
    const now = new Date("2026-07-25T12:00:00.000Z");

    await leadsService.appendRow(
      { ...validSubmission, date: "2026-08-01T10:00", notes: "Leak near the chimney." },
      now
    );

    expect(fakePool.query).toHaveBeenCalledTimes(1);
    const [sql, params] = fakePool.query.mock.calls[0];
    expect(sql).toMatch(/INSERT INTO public\.leads/);
    expect(params).toEqual([
      "2026-07-25T12:00:00.000Z",
      "Jane Smith",
      "(989) 843-4628",
      "jane@example.com",
      "Residential Roofing",
      "Alabama",
      "35203",
      "2026-08-01T10:00",
      "Leak near the chimney.",
    ]);
  });

  it("appendRow sends null for date/notes when absent", async () => {
    const fakePool = { query: vi.fn().mockResolvedValue({ rows: [] }) };
    const leadsService = createLeadsService({ env: baseEnv, pool: fakePool });
    const now = new Date("2026-07-25T12:00:00.000Z");

    await leadsService.appendRow(validSubmission, now);

    const [, params] = fakePool.query.mock.calls[0];
    expect(params[7]).toBeNull();
    expect(params[8]).toBeNull();
  });

  it("isDuplicate throws 'Lead database is not configured.' when pool is null and DATABASE_URL is unset", async () => {
    const leadsService = createLeadsService({ env: {}, pool: null });

    await expect(leadsService.isDuplicate("jane@example.com")).rejects.toThrow(
      "Lead database is not configured."
    );
  });

  it("appendRow throws 'Lead database is not configured.' when pool is null and DATABASE_URL is unset", async () => {
    const leadsService = createLeadsService({ env: {}, pool: null });

    await expect(leadsService.appendRow(validSubmission)).rejects.toThrow(
      "Lead database is not configured."
    );
  });
});
