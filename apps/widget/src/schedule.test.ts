import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "./config";

const authHeaderMock = vi.fn<() => { Authorization: string } | null>();

vi.mock("./session", () => ({
  authHeader: () => authHeaderMock(),
}));

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
};

function jsonResponse(status: number, body: unknown, extraHeaders?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}

describe("fetchSlots", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    authHeaderMock.mockReset();
    authHeaderMock.mockReturnValue({ Authorization: "Bearer jwt.abc.def" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns a typed Slot[] preserving the raw UTC starts_at strings verbatim", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, [
        { starts_at: "2026-07-20T09:00:00+00:00", ends_at: "2026-07-20T09:30:00+00:00" },
        { starts_at: "2026-07-20T10:00:00+00:00", ends_at: "2026-07-20T10:30:00+00:00" },
      ]),
    );
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.slots).toEqual([
      { startsAt: "2026-07-20T09:00:00+00:00", endsAt: "2026-07-20T09:30:00+00:00" },
      { startsAt: "2026-07-20T10:00:00+00:00", endsAt: "2026-07-20T10:30:00+00:00" },
    ]);
  });

  it("returns an empty list (ok, not an error) on a mocked 200 []", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.slots).toEqual([]);
  });

  it("sends Authorization: Bearer, credentials: omit, and no tenant_id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    const { fetchSlots } = await import("./schedule");

    await fetchSlots(baseConfig, {});

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/schedule/slots");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({ Authorization: "Bearer jwt.abc.def" });
    expect(JSON.stringify(init)).not.toContain("tenant_id");
  });

  it("appends date_from/date_to query params when provided", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    const { fetchSlots } = await import("./schedule");

    await fetchSlots(baseConfig, { dateFrom: "2026-07-20", dateTo: "2026-07-27" });

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/schedule/slots?date_from=2026-07-20&date_to=2026-07-27");
  });

  it("returns a typed ScheduleError on a mocked 401 (no throw)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { error_code: "AUTHENTICATION_ERROR", message: "Invalid token.", correlation_id: "corr-3" }),
    );
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("AUTHENTICATION_ERROR");
    expect(result.error.status).toBe(401);
  });

  it("returns a typed INVALID_RESPONSE_SHAPE error when the 200 body fails Zod validation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ starts_at: "2026-07-20T09:00:00+00:00" }]));
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });

  it("returns a typed NETWORK_ERROR (no throw) when fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
    expect(result.error.status).toBeNull();
  });

  it("returns a typed NO_SESSION error and issues no fetch when authHeader() is null", async () => {
    authHeaderMock.mockReturnValue(null);
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NO_SESSION");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a 429 with a readable Retry-After header yields errorCode RATE_LIMITED and the parsed retryAfterSeconds (S14.6 decision 3)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        429,
        { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-9" },
        { "Retry-After": "5" },
      ),
    );
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.retryAfterSeconds).toBe(5);
  });

  it("a 429 WITHOUT a readable Retry-After header yields retryAfterSeconds:null", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(429, { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-10" }),
    );
    const { fetchSlots } = await import("./schedule");

    const result = await fetchSlots(baseConfig, {});

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.retryAfterSeconds).toBeNull();
  });
});

describe("bookSlot", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    authHeaderMock.mockReset();
    authHeaderMock.mockReturnValue({ Authorization: "Bearer jwt.abc.def" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("issues one POST echoing the exact starts_at, a valid timezone, truthful consent, and no tenant_id", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        event_id: "evt-1",
        starts_at: "2026-07-20T09:00:00+00:00",
        ends_at: "2026-07-20T09:30:00+00:00",
        status: "booked",
      }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    const result = await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "America/New_York",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.booking).toEqual({
      eventId: "evt-1",
      startsAt: "2026-07-20T09:00:00+00:00",
      endsAt: "2026-07-20T09:30:00+00:00",
      status: "booked",
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/schedule/book");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer jwt.abc.def",
    });

    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody.starts_at).toBe("2026-07-20T09:00:00+00:00");
    expect(parsedBody.timezone).toBe("America/New_York");
    expect(parsedBody.consent).toEqual({
      granted: true,
      purpose: SCHEDULE_CONSENT_PURPOSE,
      text: SCHEDULE_CONSENT_TEXT,
    });
    expect((parsedBody.consent as { text: string }).text).toBe(SCHEDULE_CONSENT_TEXT);
    expect(parsedBody).not.toHaveProperty("tenant_id");
    expect(parsedBody).not.toHaveProperty("lead_id");
  });

  it("includes lead_id when provided", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        event_id: "evt-1",
        starts_at: "2026-07-20T09:00:00+00:00",
        ends_at: "2026-07-20T09:30:00+00:00",
        status: "booked",
      }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
      leadId: "lead-42",
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody.lead_id).toBe("lead-42");
  });

  it("includes phone when provided, omits it when absent", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        event_id: "evt-1",
        starts_at: "2026-07-20T09:00:00+00:00",
        ends_at: "2026-07-20T09:30:00+00:00",
        status: "booked",
      }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
      phone: "+1 555-0100",
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody.phone).toBe("+1 555-0100");
  });

  it("omits phone entirely when not provided (never sends an empty/undefined phone field)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        event_id: "evt-1",
        starts_at: "2026-07-20T09:00:00+00:00",
        ends_at: "2026-07-20T09:30:00+00:00",
        status: "booked",
      }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).not.toHaveProperty("phone");
  });

  it.each(["SLOT_UNAVAILABLE", "CONSENT_REQUIRED", "CALENDAR_SYNC_FAILED"])(
    "returns a typed ScheduleError on a mocked 422 %s (no throw, no fabricated booking)",
    async (errorCode) => {
      fetchMock.mockResolvedValueOnce(
        jsonResponse(422, {
          error_code: errorCode,
          message: "Failed.",
          correlation_id: "corr-1",
        }),
      );
      const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

      const result = await bookSlot(baseConfig, {
        startsAt: "2026-07-20T09:00:00+00:00",
        timezone: "UTC",
        consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
      });

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe(errorCode);
      expect(result.error.correlationId).toBe("corr-1");
      expect(result.error.status).toBe(422);
    },
  );

  it("returns a typed ScheduleError on a mocked 401", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { error_code: "AUTHENTICATION_ERROR", message: "Invalid token.", correlation_id: "corr-3" }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    const result = await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("AUTHENTICATION_ERROR");
    expect(result.error.status).toBe(401);
  });

  it("returns a typed INVALID_RESPONSE_SHAPE error when the 201 body fails Zod validation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { status: "booked" }));
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    const result = await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });

  it("returns a typed NETWORK_ERROR (no throw) when fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    const result = await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
    expect(result.error.status).toBeNull();
  });

  it("returns a typed NO_SESSION error and issues no fetch when authHeader() is null", async () => {
    authHeaderMock.mockReturnValue(null);
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    const result = await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NO_SESSION");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a 429 with a readable Retry-After header yields errorCode RATE_LIMITED and the parsed retryAfterSeconds (S14.6 decision 3)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        429,
        { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-11" },
        { "Retry-After": "9" },
      ),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    const result = await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("RATE_LIMITED");
    expect(result.error.retryAfterSeconds).toBe(9);
  });

  it("a 429 WITHOUT a readable Retry-After header yields retryAfterSeconds:null", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(429, { error_code: "RATE_LIMITED", message: "Too many requests.", correlation_id: "corr-12" }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    const result = await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.retryAfterSeconds).toBeNull();
  });

  it("SR-5: sends email/name when present, omits them when absent", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        event_id: "evt-1",
        starts_at: "2026-07-20T09:00:00+00:00",
        ends_at: "2026-07-20T09:30:00+00:00",
        status: "booked",
      }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
      email: "invite@example.com",
      name: "Visitor Name",
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody.email).toBe("invite@example.com");
    expect(parsedBody.name).toBe("Visitor Name");
  });

  it("SR-5: omits email/name from the request body when absent", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        event_id: "evt-1",
        starts_at: "2026-07-20T09:00:00+00:00",
        ends_at: "2026-07-20T09:30:00+00:00",
        status: "booked",
      }),
    );
    const { bookSlot, SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } = await import("./schedule");

    await bookSlot(baseConfig, {
      startsAt: "2026-07-20T09:00:00+00:00",
      timezone: "UTC",
      consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).not.toHaveProperty("email");
    expect(parsedBody).not.toHaveProperty("name");
  });
});

describe("fetchAvailabilitySummary", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    authHeaderMock.mockReset();
    authHeaderMock.mockReturnValue({ Authorization: "Bearer jwt.abc.def" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns a typed, camelCased AvailabilitySummary on a mocked 200 (schedule_cta)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        action: "schedule_cta",
        timezone: "America/New_York",
        days: [{ date: "2026-07-22", has_availability: true }, { date: "2026-07-23", has_availability: false }],
        transition_message: "I'd be happy to help you find a time with our sales team.",
        existing_booking: null,
      }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.summary).toEqual({
      action: "schedule_cta",
      timezone: "America/New_York",
      days: [
        { date: "2026-07-22", hasAvailability: true },
        { date: "2026-07-23", hasAvailability: false },
      ],
      transitionMessage: "I'd be happy to help you find a time with our sales team.",
      existingBooking: null,
    });
  });

  it("parses a non-null existing_booking", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        action: "schedule_cta",
        timezone: "UTC",
        days: [],
        transition_message: "Hi",
        existing_booking: { starts_at: "2026-07-22T09:00:00+00:00", ends_at: "2026-07-22T09:30:00+00:00", timezone: "UTC" },
      }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.summary.existingBooking).toEqual({
      startsAt: "2026-07-22T09:00:00+00:00",
      endsAt: "2026-07-22T09:30:00+00:00",
      timezone: "UTC",
    });
  });

  it("parses a response where scheduling_url is explicit JSON null (the real Pydantic wire shape for a non-Calendly tenant), never INVALID_RESPONSE_SHAPE", async () => {
    // Pydantic serializes an unset `str | None = None` field as an EXPLICIT
    // `null` key, not an omitted key -- distinct from the other tests in
    // this suite that simply leave scheduling_url out of the mock body.
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        action: "schedule_cta",
        timezone: "UTC",
        days: [{ date: "2026-07-22", has_availability: true }],
        transition_message: "Hi",
        existing_booking: null,
        scheduling_url: null,
      }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.summary.schedulingUrl).toBeUndefined();
  });

  it("returns the action:lead_form path with an empty days map (honest 200, not an error)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        action: "lead_form",
        timezone: "UTC",
        days: [],
        transition_message: "Hi",
        existing_booking: null,
      }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.summary.action).toBe("lead_form");
    expect(result.summary.days).toEqual([]);
  });

  it("sends GET with Authorization: Bearer, credentials: omit, no tenant_id, no request body", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { action: "lead_form", timezone: "UTC", days: [], transition_message: "Hi", existing_booking: null }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    await fetchAvailabilitySummary(baseConfig);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/schedule/availability-summary");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({ Authorization: "Bearer jwt.abc.def" });
    expect(init.body).toBeUndefined();
  });

  it("returns a typed NO_SESSION error and issues no fetch when no visitor session is held", async () => {
    authHeaderMock.mockReturnValue(null);
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NO_SESSION");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns a typed NETWORK_ERROR on a rejected fetch (never throws)", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
  });

  it("returns a typed error on a non-2xx error envelope", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(500, { error_code: "INTERNAL_ERROR", message: "Boom.", correlation_id: "corr-99" }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INTERNAL_ERROR");
    expect(result.error.correlationId).toBe("corr-99");
    expect(result.error.status).toBe(500);
  });

  it("returns a typed INVALID_RESPONSE_SHAPE error on a shape-mismatched 200 (never throws)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { action: "not-a-valid-action", timezone: "UTC" }));
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });

  it("SR-6: parses action=calendly_handoff with schedulingUrl", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        action: "calendly_handoff",
        timezone: "UTC",
        days: [],
        transition_message: "I'd be happy to help you find a time with our sales team.",
        existing_booking: null,
        scheduling_url: "https://calendly.com/acme/intro",
      }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.summary.action).toBe("calendly_handoff");
    expect(result.summary.schedulingUrl).toBe("https://calendly.com/acme/intro");
    expect(result.summary.days).toEqual([]);
  });

  it("SR-6: schedulingUrl is absent (undefined) for schedule_cta/lead_form actions", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        action: "schedule_cta",
        timezone: "UTC",
        days: [],
        transition_message: "Hi",
        existing_booking: null,
      }),
    );
    const { fetchAvailabilitySummary } = await import("./schedule");

    const result = await fetchAvailabilitySummary(baseConfig);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.summary.schedulingUrl).toBeUndefined();
  });
});

describe("postHandoffIntent", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    authHeaderMock.mockReset();
    authHeaderMock.mockReturnValue({ Authorization: "Bearer jwt.abc.def" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("happy path: posts {email}, Bearer auth, credentials:omit, no tenant_id/visitor_id, returns ok:true", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { recorded: true }));
    const { postHandoffIntent } = await import("./schedule");

    const result = await postHandoffIntent(baseConfig, { email: "a@example.com" });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.recorded).toBe(true);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/public/schedule/handoff-intent");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer jwt.abc.def",
    });
    const parsedBody = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsedBody).toEqual({ email: "a@example.com" });
    expect(parsedBody).not.toHaveProperty("tenant_id");
    expect(parsedBody).not.toHaveProperty("visitor_id");
  });

  it("returns a typed NO_SESSION error and issues no fetch when no visitor session is held", async () => {
    authHeaderMock.mockReturnValue(null);
    const { postHandoffIntent } = await import("./schedule");

    const result = await postHandoffIntent(baseConfig, { email: "a@example.com" });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NO_SESSION");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns a typed NETWORK_ERROR on a rejected fetch (never throws)", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { postHandoffIntent } = await import("./schedule");

    const result = await postHandoffIntent(baseConfig, { email: "a@example.com" });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("NETWORK_ERROR");
  });

  it("returns a typed error on a non-2xx error envelope", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, { error_code: "VALIDATION_ERROR", message: "Invalid email.", correlation_id: "corr-1" }),
    );
    const { postHandoffIntent } = await import("./schedule");

    const result = await postHandoffIntent(baseConfig, { email: "not-an-email" });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("VALIDATION_ERROR");
    expect(result.error.correlationId).toBe("corr-1");
  });

  it("returns a typed INVALID_RESPONSE_SHAPE error on a shape-mismatched 200 (never throws)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { recorded: "not-a-boolean" }));
    const { postHandoffIntent } = await import("./schedule");

    const result = await postHandoffIntent(baseConfig, { email: "a@example.com" });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
  });
});
