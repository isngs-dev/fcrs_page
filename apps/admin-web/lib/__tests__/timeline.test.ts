import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const { buildTimelineQuery, toTimelineResult, getContactTimeline, getLeadTimeline } = await import(
  "@/lib/timeline"
);

describe("buildTimelineQuery", () => {
  it("omits `before` when not supplied -- no invented default cursor", () => {
    const qs = buildTimelineQuery({});
    expect(qs).toBe("");
  });

  it("passes `before` through verbatim when supplied", () => {
    const qs = buildTimelineQuery({ before: "2026-07-01T00:00:00Z" });
    const params = new URLSearchParams(qs);
    expect(params.get("before")).toBe("2026-07-01T00:00:00Z");
  });

  it("passes `limit` through when supplied", () => {
    const qs = buildTimelineQuery({ limit: 25 });
    expect(new URLSearchParams(qs).get("limit")).toBe("25");
  });

  it("never carries a tenant_id (D7)", () => {
    expect(buildTimelineQuery({ before: "x", limit: 10 })).not.toMatch(/tenant/i);
  });
});

// The single highest-value test in this sprint (D4): `degraded: true` and
// per-source failure state MUST survive the mapping layer verbatim -- never
// flattened to "ok", never dropped.
describe("toTimelineResult -- D4 no-silent-fallback (MANDATORY)", () => {
  it("preserves degraded:true and a failed source's state verbatim", () => {
    const body = {
      subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
      degraded: true,
      sources: {
        bookings: { state: "failed", count: 0, truncated: false },
        conversations: { state: "ok", count: 3, truncated: false },
      },
      items: [
        { kind: "conversation", occurred_at: "2026-07-01T00:00:00Z", id: "conv-1", data: { channel: "widget" } },
      ],
      next_before: null,
    };

    const result = toTimelineResult(body);

    // The load-bearing assertion: degraded must be true, not coerced.
    expect(result.degraded).toBe(true);
    expect(result.sources.bookings.state).toBe("failed");
    expect(result.sources.conversations.state).toBe("ok");
    // The items that DID succeed are still present -- graceful degradation,
    // not an all-or-nothing failure (SR-9.3 D2, which D4 must not override).
    expect(result.items).toHaveLength(1);
  });

  it("degraded:false round-trips to false -- the disclosure means something when it appears", () => {
    const body = {
      subject: { kind: "lead", id: "lead-1", converted_to_contact_id: null },
      degraded: false,
      sources: { bookings: { state: "ok", count: 1, truncated: false } },
      items: [],
      next_before: null,
    };

    const result = toTimelineResult(body);
    expect(result.degraded).toBe(false);
  });

  it("preserves ALL source entries, including multiple failures, none dropped", () => {
    const body = {
      subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
      degraded: true,
      sources: {
        bookings: { state: "failed", count: 0, truncated: false },
        crm_sync: { state: "failed", count: 0, truncated: false },
        conversations: { state: "ok", count: 2, truncated: false },
      },
      items: [],
      next_before: null,
    };

    const result = toTimelineResult(body);
    expect(Object.keys(result.sources)).toHaveLength(3);
    expect(result.sources.bookings.state).toBe("failed");
    expect(result.sources.crm_sync.state).toBe("failed");
  });

  it("preserves converted_to_contact_id on a lead subject (D2: a converted lead does not silently widen)", () => {
    const body = {
      subject: { kind: "lead", id: "lead-1", converted_to_contact_id: "contact-9" },
      degraded: false,
      sources: {},
      items: [],
      next_before: null,
    };

    const result = toTimelineResult(body);
    expect(result.subject.convertedToContactId).toBe("contact-9");
  });

  it("preserves next_before for cursor pagination", () => {
    const body = {
      subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
      degraded: false,
      sources: {},
      items: [],
      next_before: "2026-06-01T00:00:00Z",
    };

    const result = toTimelineResult(body);
    expect(result.nextBefore).toBe("2026-06-01T00:00:00Z");
  });

  it("maps item fields snake->camel field-by-field", () => {
    const body = {
      subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
      degraded: false,
      sources: {},
      items: [{ kind: "booking", occurred_at: "2026-07-02T00:00:00Z", id: "booking-1", data: { status: "confirmed" } }],
      next_before: null,
    };

    const result = toTimelineResult(body);
    expect(result.items[0]).toEqual({
      kind: "booking",
      occurredAt: "2026-07-02T00:00:00Z",
      id: "booking-1",
      data: { status: "confirmed" },
    });
  });
});

describe("getContactTimeline / getLeadTimeline", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("getContactTimeline targets /admin/contacts/{id}/timeline, no tenant_id (D7)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
          degraded: false,
          sources: {},
          items: [],
          next_before: null,
        }),
        { status: 200 }
      )
    );

    const result = await getContactTimeline("contact-1");
    expect(result.status).toBe("ok");

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/contacts/contact-1/timeline");
    expect(url).not.toMatch(/tenant/i);
  });

  it("getLeadTimeline targets /admin/leads/{id}/timeline, no tenant_id (D7)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          subject: { kind: "lead", id: "lead-1", converted_to_contact_id: null },
          degraded: false,
          sources: {},
          items: [],
          next_before: null,
        }),
        { status: 200 }
      )
    );

    await getLeadTimeline("lead-1", { before: "2026-07-01T00:00:00Z" });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/leads/lead-1/timeline?before=2026-07-01T00%3A00%3A00Z");
    expect(url).not.toMatch(/tenant/i);
  });

  it("a degraded response from the wire is NOT cached client-side (no caching layer exists here at all -- the backend owns that per M5) and still returns status 'ok' with degraded:true surfaced to the caller", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
          degraded: true,
          sources: { bookings: { state: "failed", count: 0, truncated: false } },
          items: [],
          next_before: null,
        }),
        { status: 200 }
      )
    );

    const result = await getContactTimeline("contact-1");
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.data.degraded).toBe(true);
    }
  });

  it("maps a 404 to a not-found message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "NOT_FOUND", message: "x", correlation_id: "c" }), { status: 404 })
    );

    const result = await getContactTimeline("contact-1");
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/not be found/i);
  });

  it("maps a network throw to a generic message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await getLeadTimeline("lead-1");
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/unable to reach/i);
  });

  it("never logs the response body (PII/content-minimal)", async () => {
    getMock.mockReturnValue(undefined);
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
          degraded: false,
          sources: {},
          items: [{ kind: "note", occurred_at: "2026-07-01T00:00:00Z", id: "note-1", data: { text: "secret" } }],
          next_before: null,
        }),
        { status: 200 }
      )
    );

    await getContactTimeline("contact-1");

    expect(consoleSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });
});
