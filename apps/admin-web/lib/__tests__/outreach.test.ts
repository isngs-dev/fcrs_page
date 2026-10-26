/** A8: the Outreach data layer (mapping, paths, errors) and its pure helpers. */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  camelize,
  dollarsToMicros,
  formatDuration,
  humanize,
  isEmailFilter,
  microsToDollars,
} from "@/lib/outreach-format";

const getMock = vi.fn();
vi.mock("next/headers", () => ({ cookies: vi.fn(async () => ({ get: getMock })) }));

const outreach = await import("@/lib/outreach");

function respond(body: unknown, status = 200) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status }));
}

function lastRequest(spy: ReturnType<typeof respond>): { url: string; init: RequestInit } {
  const [url, init] = spy.mock.calls.at(-1)!;
  return { url: String(url), init: init as RequestInit };
}

describe("outreach-format", () => {
  it("camelizes nested keys without touching values", () => {
    expect(camelize({ cost_by_kind: { telephony: 1 }, rows: [{ e164_value: "+1" }] })).toEqual({
      costByKind: { telephony: 1 },
      rows: [{ e164Value: "+1" }],
    });
  });

  it("converts dollars and micros both ways (U12)", () => {
    expect(microsToDollars(150_000_000)).toBe("150.00");
    expect(microsToDollars(null)).toBe("");
    expect(dollarsToMicros("$150.5")).toBe(150_500_000);
    expect(dollarsToMicros(" ")).toBeNull();
    expect(dollarsToMicros("ten")).toBeUndefined();
    expect(dollarsToMicros("1.234")).toBeUndefined();
  });

  it("formats durations, labels and filters", () => {
    expect(formatDuration(75)).toBe("1m 15s");
    expect(formatDuration(9)).toBe("9s");
    expect(formatDuration(null)).toBe("—");
    expect(humanize("not_interested")).toBe("Not interested");
    expect(humanize("INVALID_EMAIL")).toBe("Invalid email");
    expect(humanize("planPlanned")).toBe("Plan planned");
    expect(isEmailFilter("draft")).toBe(true);
    expect(isEmailFilter("bogus")).toBe(false);
    expect(isEmailFilter(undefined)).toBe(false); // no param = the Drafts default, not "All"
  });
});

describe("lib/outreach", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("reads the overview from the implicit route with the caller's cookie, camelCased", async () => {
    getMock.mockReturnValue({ value: "jwt-1" });
    const spy = respond({ outreach_paused: false, drafts_awaiting: 2, costs: { month_micros: 5, by_kind: {} } });
    const result = await outreach.getOverview();
    const { url, init } = lastRequest(spy);
    expect(url).toBe("http://localhost:8000/admin/outreach/overview");
    expect(new Headers(init.headers).get("Cookie")).toBe("access_token=jwt-1");
    expect(result).toMatchObject({ status: "ok", overview: { draftsAwaiting: 2, costs: { monthMicros: 5 } } });
  });

  it("never addresses a tenant in the path (U2)", async () => {
    const spy = respond([]);
    await outreach.listEmails("draft");
    await outreach.listCalls();
    await outreach.getLeadActivity("lead 1");
    for (const [url] of spy.mock.calls) expect(String(url)).not.toContain("/admin/tenants/");
    expect(String(spy.mock.calls[0][0])).toContain("/admin/outreach/emails?status=draft");
    expect(String(spy.mock.calls[2][0])).toContain("/admin/outreach/leads/lead%201/activity");
  });

  it("posts the right mutations", async () => {
    const spy = respond({ event_id: "e1" }, 202);
    expect(await outreach.takeOver("c-1")).toEqual({ status: "ok", result: { eventId: "e1" } });
    expect(lastRequest(spy)).toMatchObject({ url: expect.stringContaining("/contacts/c-1:takeover"), init: { method: "POST" } });

    await outreach.saveDraft("m-1", "Hi", "Body");
    const { url, init } = lastRequest(spy);
    expect(url).toContain("/emails/m-1");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ subject: "Hi", body_text: "Body" });

    const form = new FormData();
    form.set("campaign_name", "Spring");
    await outreach.uploadImport(form);
    expect(lastRequest(spy).init.body).toBe(form); // multipart passes straight through
  });

  it("handles a 204 without a body", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    expect(await outreach.removeSuppression("s-1")).toEqual({ status: "ok", result: null });
  });

  it("shows the backend's user-safe message for 4xx, a correlation id for 5xx", async () => {
    respond({ error_code: "EMAIL_NOT_READY", message: "Set up the email sender first.", correlation_id: "c1" }, 422);
    expect(await outreach.saveAgentSettings({})).toEqual({
      status: "error",
      message: "Set up the email sender first.",
      errorCode: "EMAIL_NOT_READY",
      correlationId: "c1",
    });
    respond({ error_code: "INTERNAL_ERROR", message: "boom", correlation_id: "c2" }, 500);
    const failed = await outreach.getOverview();
    expect(failed.status === "error" && failed.message).toContain("Correlation ID: c2");
    respond({ error_code: "ROLE_NOT_PERMITTED", message: "x", correlation_id: "c3" }, 403);
    const denied = await outreach.listCampaigns();
    expect(denied.status === "error" && denied.message).toBe("You do not have permission to do this.");
  });

  it("reports an unreachable server honestly", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("fetch failed"));
    expect(await outreach.getOverview()).toMatchObject({ status: "error", message: "Unable to reach the server. Please try again." });
  });
});

describe("campaign end date (A12 F5)", () => {
  it("turns a picked day into the end of that day, and rejects junk", async () => {
    const { endOfDayIso } = await import("@/app/(protected)/outreach/campaigns/campaign-end-date");
    const iso = endOfDayIso("2026-12-31");
    expect(iso && new Date(iso).getHours()).toBe(23);
    expect(endOfDayIso("31/12/2026")).toBeNull();
  });
});

describe("percent (A13)", () => {
  it("shows a rate, or a dash when there is nothing to divide by", async () => {
    const { percent } = await import("@/lib/outreach-format");
    expect(percent(0.25)).toBe("25%");
    expect(percent(0.054)).toBe("5.4%");
    expect(percent(null)).toBe("—");
  });
});
